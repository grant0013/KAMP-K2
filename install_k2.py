#!/usr/bin/env python3
"""
KAMP-K2 auto-installer.

Installs KAMP + the K2-specific `restore_bed_mesh` override onto a stock
Creality K2 / K2 Plus over SSH. Idempotent: safe to re-run.

Usage:
    python install_k2.py --host 192.168.3.57
    python install_k2.py --host 192.168.3.57 --password MYPASS
    python install_k2.py --host 192.168.3.57 --dry-run
    python install_k2.py --host 192.168.3.57 --revert

Defaults:
    user=root, password=creality_2024 (Creality stock credential)

What this does:
    1. Copies extras/restore_bed_mesh.py -> /usr/share/klipper/klippy/extras/
    2. Copies Configuration/{KAMP_Settings,Adaptive_Meshing,Line_Purge}.cfg
       -> /mnt/UDISK/printer_data/config/KAMP/
    3. Fixes KAMP_Settings.cfg include paths (relative to file, not config root).
    4. Uncomments Adaptive_Meshing + Line_Purge in KAMP_Settings.cfg.
    5. Adds `[include KAMP/KAMP_Settings.cfg]` and `[restore_bed_mesh]` to
       printer.cfg (if absent).
    6. Hijacks `[gcode_macro G29]` and `[gcode_macro BED_MESH_CALIBRATE_START_PRINT]`
       to no-op handshake macros (master-server compatibility).
    7. Inserts a bare `BED_MESH_CALIBRATE` call (KAMP picks it up) and
       `LINE_PURGE` call into `[gcode_macro START_PRINT]`.
    8. Backs up originals to /mnt/exUDISK/.system/kamp_k2_backup_<timestamp>/
       (firmware-update-survivable) if the SSD is present, else to
       /mnt/UDISK/printer_data/config/backups/.
    9. Restarts Klippy and checks the log for the expected "KAMP mode" message.

Requires: paramiko (`pip install paramiko`).
"""
from __future__ import annotations

import argparse
import io
import os
import posixpath
import re
import socket
import sys
import time

try:
    import paramiko
except ImportError:
    sys.stderr.write(
        "error: paramiko is required. Install with: pip install paramiko\n")
    sys.exit(2)


# ---------- constants --------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

# (local_path_relative_to_repo, remote_path, mode)
FILES_TO_COPY = [
    ("extras/restore_bed_mesh.py",
     "/usr/share/klipper/klippy/extras/restore_bed_mesh.py", 0o644),
    ("Configuration/KAMP_Settings.cfg",
     "/mnt/UDISK/printer_data/config/KAMP/KAMP_Settings.cfg", 0o644),
    ("Configuration/Adaptive_Meshing.cfg",
     "/mnt/UDISK/printer_data/config/KAMP/Adaptive_Meshing.cfg", 0o644),
    ("Configuration/Line_Purge.cfg",
     "/mnt/UDISK/printer_data/config/KAMP/Line_Purge.cfg", 0o644),
]

PRINTER_CFG = "/mnt/UDISK/printer_data/config/printer.cfg"
GCODE_MACRO_CFG = "/mnt/UDISK/printer_data/config/gcode_macro.cfg"

# What we add to printer.cfg if absent (after [exclude_object] is best).
PRINTER_CFG_SNIPPET = """
[include KAMP/KAMP_Settings.cfg]
[restore_bed_mesh]
"""

G29_HIJACK = """[gcode_macro G29]
description: Hijacked by KAMP-K2 - defers mesh to START_PRINT (adaptive). Emits fake [G29_TIME] so master-server is satisfied.
gcode:
  {% set bed_temp = params.BED_TEMP|default(0)|float %}
  {% if bed_temp > 0 %}
    M140 S{bed_temp}
  {% endif %}
  {% if "xy" not in printer.toolhead.homed_axes %}
    G28 X Y
  {% endif %}
  BED_MESH_CLEAR
  M118 G29 deferred: real mesh runs in START_PRINT (KAMP adaptive)
  M118 [G29_TIME]Execution time: 0.0 seconds, Time spent at each point: 0.0
"""

BMCSP_HIJACK = """[gcode_macro BED_MESH_CALIBRATE_START_PRINT]
description: Hijacked by KAMP-K2 - defers mesh to START_PRINT (adaptive). Emits fake [G29_TIME] so master-server is satisfied.
gcode:
  BED_MESH_CLEAR
  M118 BED_MESH_CALIBRATE_START_PRINT deferred (KAMP adaptive in START_PRINT)
  M118 [G29_TIME]Execution time: 0.0 seconds, Time spent at each point: 0.0
"""

START_PRINT_MESH_BLOCK = """  # KAMP-K2: adaptive mesh. BED_MESH_CALIBRATE is wrapped by KAMP and
  # reads exclude_object metadata from the loaded gcode to size the probe
  # to just the print area.
  {% if params.MESH|default(1)|int == 1 %}
    BED_MESH_CLEAR
    BED_MESH_CALIBRATE
  {% else %}
    M118 Mesh skipped (MESH=0 from slicer)
  {% endif %}"""

LINE_PURGE_LINE = "  # KAMP-K2: adaptive purge line at print-area edge\n  LINE_PURGE\n"


# ---------- helpers ----------------------------------------------------------

class Installer:
    def __init__(self, host: str, user: str, password: str,
                 dry_run: bool = False, verbose: bool = False) -> None:
        self.host = host
        self.user = user
        self.password = password
        self.dry_run = dry_run
        self.verbose = verbose
        self.ssh: paramiko.SSHClient | None = None
        self.changes_made: list[str] = []

    # ---- low-level ----
    def log(self, msg: str, level: str = "info") -> None:
        prefix = {"info": " ", "ok": "+", "warn": "!", "err": "x",
                  "step": "»", "dry": "~"}.get(level, " ")
        print(f"[{prefix}] {msg}")

    def connect(self) -> None:
        self.log(f"Connecting to {self.user}@{self.host}...", "step")
        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            self.ssh.connect(self.host, username=self.user,
                             password=self.password, timeout=10,
                             allow_agent=False, look_for_keys=False)
        except paramiko.AuthenticationException:
            self.log(f"SSH auth failed for {self.user}@{self.host}", "err")
            self.log("If you changed root's password, pass it with --password",
                     "err")
            sys.exit(1)
        except (socket.timeout, socket.error) as e:
            self.log(f"Cannot reach {self.host}: {e}", "err")
            sys.exit(1)
        # NOTE: K2 runs dropbear, which does NOT support the SFTP subsystem.
        # All file I/O has to go through exec_command + base64 shell pipes.
        # (Attempting ssh.open_sftp() here would raise EOFError.)
        self.log(f"Connected.", "ok")

    def close(self) -> None:
        if self.ssh:
            self.ssh.close()

    def run(self, cmd: str) -> tuple[int, str, str]:
        assert self.ssh is not None
        stdin, stdout, stderr = self.ssh.exec_command(cmd)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        rc = stdout.channel.recv_exit_status()
        if self.verbose:
            self.log(f"  $ {cmd}  (rc={rc})", "info")
            if out.strip():
                self.log(f"    out: {out.strip()[:300]}", "info")
            if err.strip():
                self.log(f"    err: {err.strip()[:300]}", "info")
        return rc, out, err

    def remote_exists(self, path: str) -> bool:
        _, out, _ = self.run(f"test -e '{path}' && echo YES || echo NO")
        return "YES" in out

    def read_remote(self, path: str) -> str:
        # Read file via `cat` over exec_command. K2 busybox doesn't have
        # base64, so we stream raw bytes back through the SSH channel.
        assert self.ssh is not None
        stdin, stdout, stderr = self.ssh.exec_command(f"cat '{path}'")
        data = stdout.read()
        err = stderr.read().decode("utf-8", errors="replace")
        rc = stdout.channel.recv_exit_status()
        if rc != 0:
            raise FileNotFoundError(
                f"read_remote {path} failed: {err.strip()}")
        return data.decode("utf-8", errors="replace")

    def write_remote(self, path: str, content: str, mode: int = 0o644) -> None:
        if self.dry_run:
            self.log(f"[dry-run] would write {path} "
                     f"({len(content)} bytes)", "dry")
            return
        assert self.ssh is not None
        parent = posixpath.dirname(path)
        if parent:
            self.run(f"mkdir -p '{parent}'")
        octal_mode = oct(mode)[2:]
        raw = content.encode() if isinstance(content, str) else content
        # Pipe content through `cat > file` via exec_command stdin.
        # Binary-safe, no shell quoting concerns, works on dropbear which
        # doesn't have an SFTP server. K2 busybox also lacks base64/uuencode
        # so this is the portable path.
        stdin, stdout, stderr = self.ssh.exec_command(
            f"cat > '{path}' && chmod {octal_mode} '{path}'")
        stdin.write(raw)
        stdin.channel.shutdown_write()
        err = stderr.read().decode("utf-8", errors="replace")
        rc = stdout.channel.recv_exit_status()
        if rc != 0:
            self.log(f"write_remote {path} failed: {err.strip()}", "err")
            raise RuntimeError(f"write_remote failed for {path}")

    def copy_file(self, local: str, remote: str, mode: int = 0o644) -> None:
        with open(local, "rb") as f:
            content = f.read().decode("utf-8", errors="replace")
        if self.dry_run:
            self.log(f"[dry-run] would copy {local} -> {remote}", "dry")
            return
        self.write_remote(remote, content, mode)

    # ---- checks ----
    def sanity_check(self) -> None:
        """Verify this is a K2-ish Creality printer before doing anything."""
        checks = [
            ("klipper config dir",
             "test -d /mnt/UDISK/printer_data/config && echo yes"),
            ("klipper extras dir",
             "test -d /usr/share/klipper/klippy/extras && echo yes"),
            ("prtouch_v3 wrapper (K2 indicator)",
             "test -f /usr/share/klipper/klippy/extras/"
             "prtouch_v3_wrapper.cpython-39.so && echo yes"),
            ("master-server binary (Creality firmware)",
             "test -f /usr/bin/master-server && echo yes"),
        ]
        fails = []
        for label, cmd in checks:
            rc, out, _ = self.run(cmd)
            if "yes" not in out:
                fails.append(label)
                self.log(f"  check failed: {label}", "err")
            else:
                self.log(f"  check ok: {label}", "ok")
        if fails:
            self.log("This does not look like a stock Creality K2. Aborting.",
                     "err")
            self.log("Missing: " + ", ".join(fails), "err")
            sys.exit(1)

    def exclude_object_section(self) -> bool:
        """Verify [exclude_object] is defined in printer.cfg — KAMP needs it."""
        cfg = self.read_remote(PRINTER_CFG)
        return re.search(r"^\[exclude_object\]", cfg, re.MULTILINE) is not None

    # ---- install steps ----
    def backup_configs(self) -> None:
        # Prefer SSD if available (firmware-update survivable)
        rc, _, _ = self.run("test -d /mnt/exUDISK && echo yes || echo no")
        ts = time.strftime("%Y%m%d_%H%M%S")
        if "yes" in self.run("test -d /mnt/exUDISK && echo yes")[1]:
            base = f"/mnt/exUDISK/.system/kamp_k2_backup_{ts}"
        else:
            base = f"/mnt/UDISK/printer_data/config/backups/kamp_k2_{ts}"
        self.log(f"Backing up current configs to {base}/", "step")
        if self.dry_run:
            self.log(f"[dry-run] would create {base} and copy configs", "dry")
            return
        self.run(f"mkdir -p '{base}'")
        self.run(f"cp {PRINTER_CFG} '{base}/printer.cfg'")
        self.run(f"cp {GCODE_MACRO_CFG} '{base}/gcode_macro.cfg'")
        self.run(f"[ -d /mnt/UDISK/printer_data/config/KAMP ] && "
                 f"cp -r /mnt/UDISK/printer_data/config/KAMP '{base}/KAMP' "
                 f"|| true")
        self.log(f"Backup saved.", "ok")

    def copy_files(self) -> None:
        self.log("Copying KAMP-K2 files to printer...", "step")
        for local_rel, remote, mode in FILES_TO_COPY:
            local = os.path.join(REPO_ROOT, local_rel)
            if not os.path.isfile(local):
                self.log(f"  missing local file: {local}", "err")
                sys.exit(1)
            self.copy_file(local, remote, mode)
            self.log(f"  {local_rel} -> {remote}", "ok")

    def fix_kamp_settings(self) -> None:
        """KAMP's distributed KAMP_Settings.cfg uses `./KAMP/...` paths that
        break when placed in a KAMP/ subdirectory. Fix paths + uncomment the
        two includes we want."""
        path = "/mnt/UDISK/printer_data/config/KAMP/KAMP_Settings.cfg"
        src = self.read_remote(path)
        src = src.replace("#[include ./KAMP/Adaptive_Meshing.cfg]",
                          "[include Adaptive_Meshing.cfg]")
        src = src.replace("#[include ./KAMP/Line_Purge.cfg]",
                          "[include Line_Purge.cfg]")
        # Already-uncommented variant from upstream
        src = src.replace("[include ./KAMP/Adaptive_Meshing.cfg]",
                          "[include Adaptive_Meshing.cfg]")
        src = src.replace("[include ./KAMP/Line_Purge.cfg]",
                          "[include Line_Purge.cfg]")
        self.write_remote(path, src)
        self.log("KAMP_Settings.cfg: include paths fixed, adaptive+purge enabled", "ok")

    def patch_printer_cfg(self) -> None:
        cfg = self.read_remote(PRINTER_CFG)
        changes = False
        if "[include KAMP/KAMP_Settings.cfg]" not in cfg:
            # Prefer after [exclude_object] for clean placement
            anchor = "[exclude_object]"
            if anchor in cfg:
                cfg = cfg.replace(
                    anchor,
                    f"{anchor}\n\n[restore_bed_mesh]\n\n"
                    f"[include KAMP/KAMP_Settings.cfg]",
                    1,
                )
                changes = True
            else:
                # Fall back to end of file
                cfg = cfg.rstrip() + PRINTER_CFG_SNIPPET + "\n"
                changes = True
        elif "[restore_bed_mesh]" not in cfg:
            cfg = cfg.replace(
                "[include KAMP/KAMP_Settings.cfg]",
                "[restore_bed_mesh]\n\n[include KAMP/KAMP_Settings.cfg]",
                1,
            )
            changes = True
        if changes:
            self.write_remote(PRINTER_CFG, cfg)
            self.log("printer.cfg: added [restore_bed_mesh] + KAMP include", "ok")
        else:
            self.log("printer.cfg: already contains includes, skipping", "ok")

    def patch_gcode_macro(self) -> None:
        cfg = self.read_remote(GCODE_MACRO_CFG)
        original = cfg

        # 1. Hijack [gcode_macro G29]
        if "Hijacked by KAMP-K2" not in re.search(
                r"^\[gcode_macro G29\].*?(?=^\[gcode_macro )",
                cfg, re.MULTILINE | re.DOTALL).group(0):
            cfg = re.sub(
                r"^\[gcode_macro G29\].*?(?=^\[gcode_macro )",
                G29_HIJACK + "\n\n",
                cfg,
                count=1,
                flags=re.MULTILINE | re.DOTALL,
            )
            self.log("gcode_macro.cfg: G29 hijacked", "ok")

        # 2. Hijack [gcode_macro BED_MESH_CALIBRATE_START_PRINT]
        m = re.search(
            r"^\[gcode_macro BED_MESH_CALIBRATE_START_PRINT\].*?(?=^\[gcode_macro )",
            cfg, re.MULTILINE | re.DOTALL)
        if m and "Hijacked by KAMP-K2" not in m.group(0):
            cfg = re.sub(
                r"^\[gcode_macro BED_MESH_CALIBRATE_START_PRINT\].*?"
                r"(?=^\[gcode_macro )",
                BMCSP_HIJACK + "\n\n",
                cfg,
                count=1,
                flags=re.MULTILINE | re.DOTALL,
            )
            self.log("gcode_macro.cfg: BED_MESH_CALIBRATE_START_PRINT hijacked", "ok")

        # 3. Insert KAMP mesh call + LINE_PURGE into START_PRINT if not already
        if "KAMP-K2: adaptive mesh" not in cfg:
            # Anchor: right after the prepare==0/1 if/else, before M140 S{params.BED_TEMP}
            anchor = re.compile(
                r"(  \{% else %\}\s*\n"
                r"    PRINT_PREPARE_CLEAR\s*\n"
                r"  \{% endif %\}\s*\n)"
                r"(  M140 S\{params\.BED_TEMP\})",
                re.MULTILINE,
            )
            if anchor.search(cfg):
                cfg = anchor.sub(
                    r"\1\n" + START_PRINT_MESH_BLOCK + "\n\n" + r"\2",
                    cfg, count=1,
                )
                self.log("START_PRINT: KAMP mesh block inserted", "ok")
            else:
                self.log("START_PRINT: anchor not found, skipping mesh block "
                         "insert (manual step may be needed)", "warn")

        if "KAMP-K2: adaptive purge" not in cfg:
            # Anchor: after BOX_NOZZLE_CLEAN, before G92 E0 ; Reset Extruder
            anchor = re.compile(
                r"(  BOX_NOZZLE_CLEAN\n)"
                r"(  G92 E0 ; Reset Extruder)"
            )
            m = anchor.search(cfg)
            if m:
                # Only replace the LAST occurrence of BOX_NOZZLE_CLEAN before G92 E0
                # (there are multiple BOX_NOZZLE_CLEAN in the macro)
                cfg = anchor.sub(
                    r"\1" + LINE_PURGE_LINE + r"\2",
                    cfg, count=1,
                )
                self.log("START_PRINT: LINE_PURGE inserted", "ok")
            else:
                self.log("START_PRINT: LINE_PURGE anchor not found "
                         "(manual step may be needed)", "warn")

        if cfg != original:
            self.write_remote(GCODE_MACRO_CFG, cfg)

    def verify_parse(self) -> None:
        """Best-effort verification: ask python3 to parse restore_bed_mesh.py."""
        rc, out, err = self.run(
            "python3 -c 'import ast; "
            "ast.parse(open(\"/usr/share/klipper/klippy/extras/"
            "restore_bed_mesh.py\").read()); print(\"ok\")'"
        )
        if "ok" not in out:
            self.log(f"restore_bed_mesh.py parse FAILED: {err}", "err")
            sys.exit(1)
        self.log("restore_bed_mesh.py parse ok", "ok")

    def restart_klippy(self) -> None:
        if self.dry_run:
            self.log("[dry-run] would FIRMWARE_RESTART Klippy", "dry")
            return
        self.log("Restarting Klippy to pick up changes...", "step")
        script = (
            "import socket, json, time\n"
            "s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
            "s.connect('/tmp/klippy_uds')\n"
            "s.send((json.dumps({'id':1,'method':'gcode/script',"
            "'params':{'script':'FIRMWARE_RESTART'}})+chr(3)).encode())\n"
            "time.sleep(0.3)\n"
        )
        self.run(f"python3 -c \"{script}\"")
        # Wait for ready
        self.log("Waiting for Klippy to come back ready (up to 60s)...", "info")
        deadline = time.time() + 60
        ready = False
        while time.time() < deadline:
            rc, out, _ = self.run(
                "python3 -c \"import socket,json,time\n"
                "s=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
                "try: s.connect('/tmp/klippy_uds')\n"
                "except: exit(1)\n"
                "s.send((json.dumps({'id':1,'method':'info'})+chr(3)).encode())\n"
                "time.sleep(0.5); buf=b''\n"
                "s.settimeout(2)\n"
                "try:\n"
                "  while True:\n"
                "    c=s.recv(65536)\n"
                "    if not c: break\n"
                "    buf+=c\n"
                "except: pass\n"
                "for fr in buf.split(chr(3).encode()):\n"
                "  if fr.strip():\n"
                "    try:\n"
                "      r=json.loads(fr)\n"
                "      print(r['result']['state'])\n"
                "      break\n"
                "    except: pass\""
            )
            state = out.strip().splitlines()[-1] if out.strip() else ""
            if state == "ready":
                ready = True
                break
            if state == "error":
                self.log("Klippy entered error state!", "err")
                rc, out, _ = self.run(
                    "tail -40 /mnt/UDISK/printer_data/logs/klippy.log "
                    "| grep -iE 'error|exception' | tail -10")
                self.log(out, "err")
                sys.exit(1)
            time.sleep(2)
        if not ready:
            self.log("Klippy did not become ready within 60s — check logs", "warn")
        else:
            self.log("Klippy ready.", "ok")

    def verify_override(self) -> None:
        rc, out, _ = self.run(
            "grep 'bed_mesh_override' /mnt/UDISK/printer_data/logs/klippy.log "
            "| tail -3"
        )
        if "KAMP mode" in out:
            self.log("Override active in KAMP mode (log confirms)", "ok")
        elif "re-registered to guarded upstream" in out:
            self.log("Override active in direct mode (KAMP not detected?)",
                     "warn")
        else:
            self.log("Override log message not found — something went wrong",
                     "warn")
            self.log(f"log tail:\n{out}", "warn")

    # ---- revert ----
    def find_latest_backup(self) -> str | None:
        """Return the path to the latest kamp_k2_backup_* dir on the printer,
        preferring /mnt/exUDISK (firmware-update-survivable) over UDISK."""
        candidates = [
            "/mnt/exUDISK/.system",
            "/mnt/UDISK/printer_data/config/backups",
        ]
        for base in candidates:
            rc, out, _ = self.run(
                f"ls -1dt '{base}'/kamp_k2_backup_* 2>/dev/null | head -1"
            )
            path = out.strip()
            if path:
                return path
        return None

    def revert(self) -> None:
        """Restore the most recent kamp_k2_backup and remove installed files."""
        self.log("=== Sanity checks ===", "step")
        self.sanity_check()

        self.log("=== Finding latest backup ===", "step")
        backup = self.find_latest_backup()
        if not backup:
            self.log("No kamp_k2_backup_* directory found on the printer.",
                     "err")
            self.log("Checked /mnt/exUDISK/.system and "
                     "/mnt/UDISK/printer_data/config/backups", "err")
            self.log("If you installed manually, restore your own backup and "
                     "remove: restore_bed_mesh.py, [restore_bed_mesh], "
                     "[include KAMP/...], KAMP/ directory.", "err")
            sys.exit(1)
        self.log(f"Using backup: {backup}", "ok")

        # Confirm contents
        rc, out, _ = self.run(f"ls '{backup}'")
        self.log(f"Backup contents: {out.strip().replace(chr(10), ', ')}",
                 "info")
        if "printer.cfg" not in out or "gcode_macro.cfg" not in out:
            self.log("Backup is missing printer.cfg or gcode_macro.cfg — "
                     "aborting to avoid a half-revert.", "err")
            sys.exit(1)

        if self.dry_run:
            self.log(f"[dry-run] would:", "dry")
            self.log(f"[dry-run]   cp {backup}/printer.cfg -> {PRINTER_CFG}",
                     "dry")
            self.log(f"[dry-run]   cp {backup}/gcode_macro.cfg -> "
                     f"{GCODE_MACRO_CFG}", "dry")
            self.log("[dry-run]   rm /usr/share/klipper/klippy/extras/"
                     "restore_bed_mesh.py", "dry")
            self.log("[dry-run]   rm -rf /mnt/UDISK/printer_data/config/KAMP",
                     "dry")
            self.log("[dry-run]   FIRMWARE_RESTART Klippy", "dry")
            return

        self.log("=== Restoring configs ===", "step")
        self.run(f"cp '{backup}/printer.cfg' {PRINTER_CFG}")
        self.run(f"cp '{backup}/gcode_macro.cfg' {GCODE_MACRO_CFG}")
        self.log("printer.cfg + gcode_macro.cfg restored from backup", "ok")

        self.log("=== Removing KAMP-K2 installed files ===", "step")
        self.run("rm -f /usr/share/klipper/klippy/extras/restore_bed_mesh.py")
        self.run("rm -rf /mnt/UDISK/printer_data/config/KAMP")
        self.log("Removed restore_bed_mesh.py + KAMP/ directory", "ok")

        self.log("=== Restart ===", "step")
        self.restart_klippy()
        rc, out, _ = self.run(
            "grep -iE 'bed_mesh_override|kamp' "
            "/mnt/UDISK/printer_data/logs/klippy.log | "
            "grep -i 'Klippy state' | tail -1"
        )
        rc, state_out, _ = self.run(
            "python3 -c \"import socket, json, time\n"
            "s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
            "s.connect('/tmp/klippy_uds')\n"
            "s.send((json.dumps({'id':1,'method':'info'})+chr(3)).encode())\n"
            "time.sleep(0.5); buf=b''; s.settimeout(2)\n"
            "try:\n"
            "  while True:\n"
            "    c=s.recv(65536)\n"
            "    if not c: break\n"
            "    buf+=c\n"
            "except: pass\n"
            "for fr in buf.split(chr(3).encode()):\n"
            "  if fr.strip():\n"
            "    try: r=json.loads(fr); print(r['result']['state']); break\n"
            "    except: pass\""
        )
        self.log(f"Klippy state after revert: {state_out.strip()}", "ok")
        self.log("Revert complete. Printer is back to pre-install state.",
                 "ok")


# ---------- main -------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Install KAMP-K2 on a Creality K2 printer over SSH.")
    ap.add_argument("--host", required=True, help="Printer IP address")
    ap.add_argument("--user", default="root", help="SSH user (default: root)")
    ap.add_argument("--password", default="creality_2024",
                    help="SSH password (default: creality_2024)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would change without writing anything")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Show remote command output")
    ap.add_argument("--revert", action="store_true",
                    help="Restore the most recent kamp_k2_backup and remove "
                         "all KAMP-K2 installed files, returning the printer "
                         "to its pre-install state.")
    args = ap.parse_args()

    inst = Installer(args.host, args.user, args.password,
                     dry_run=args.dry_run, verbose=args.verbose)
    try:
        inst.connect()

        if args.revert:
            inst.revert()
            return

        inst.log("=== Sanity checks ===", "step")
        inst.sanity_check()
        if not inst.exclude_object_section():
            inst.log("[exclude_object] not found in printer.cfg — KAMP "
                     "adaptive meshing needs it. Aborting.", "err")
            sys.exit(1)

        inst.log("=== Backup ===", "step")
        inst.backup_configs()

        inst.log("=== File copy ===", "step")
        inst.copy_files()

        inst.log("=== Config patches ===", "step")
        inst.fix_kamp_settings()
        inst.patch_printer_cfg()
        inst.patch_gcode_macro()

        inst.log("=== Verify ===", "step")
        inst.verify_parse()

        inst.log("=== Restart & verify override ===", "step")
        inst.restart_klippy()
        inst.verify_override()

        inst.log("Done. Slice a test print with EXCLUDE_OBJECT_DEFINE "
                 "enabled and watch the gcode console for 'Adapted probe "
                 "count' output.", "ok")
    finally:
        inst.close()


if __name__ == "__main__":
    main()
