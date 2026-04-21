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
    python install_k2.py --host 192.168.3.57 --board F008  # force K2 Plus path

Defaults:
    user=root, password=creality_2024 (Creality stock credential)
    board=auto (detects from printer.cfg header + [z_tilt]/[stepper_z1])

What this does:
    1. Detects board (F021 = K2/Combo/Pro single-Z, F008 = K2 Plus dual-Z).
    2. Copies extras/restore_bed_mesh.py -> /usr/share/klipper/klippy/extras/
    3. Copies Configuration/{KAMP_Settings,Adaptive_Meshing,Line_Purge}.cfg
       -> /mnt/UDISK/printer_data/config/KAMP/
    4. Fixes KAMP_Settings.cfg include paths (relative to file, not config root).
    5. Uncomments Adaptive_Meshing + Line_Purge in KAMP_Settings.cfg.
    6. Adds `[include KAMP/KAMP_Settings.cfg]` and `[restore_bed_mesh]` to
       printer.cfg (if absent).
    7. F008 only: flips `forced_leveling: true` -> `false` in [virtual_sdcard]
       to stop master-server from setting the bed_mesh_calibate_state flag
       that collides with upstream Klipper bed_mesh via our override.
    8. Hijacks `[gcode_macro G29]` and `[gcode_macro BED_MESH_CALIBRATE_START_PRINT]`
       to no-op handshake macros (master-server compatibility).
    9. Inserts a bare `BED_MESH_CALIBRATE` call into `[gcode_macro START_PRINT]`
       (KAMP picks it up and runs adaptive mesh). LINE_PURGE goes in the
       slicer start-gcode, not here -- see README "Slicer setup".
   10. Backs up originals to /mnt/exUDISK/.system/kamp_k2_backup_<timestamp>/
       (firmware-update-survivable) if the SSD is present, else to
       /mnt/UDISK/printer_data/config/backups/.
   11. Restarts Klippy and checks the log for the expected "KAMP mode" message.

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
                 dry_run: bool = False, verbose: bool = False,
                 board: str = "auto",
                 local_backup_dir: str | None = None) -> None:
        self.host = host
        self.user = user
        self.password = password
        self.dry_run = dry_run
        self.verbose = verbose
        self.board = board  # "auto" | "F008" | "F021"
        self.local_backup_dir = local_backup_dir
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

    def check_for_duplicates(self) -> None:
        """Scan all config files under printer_data/config for things that
        MUST appear exactly once. Refuse to install if any are doubled.

        Motivation: users retrying an install repeatedly can accumulate
        duplicate `[include KAMP/...]` lines, duplicate [gcode_macro]
        sections, or duplicate `rename_existing: _BMC_KAMP_INNER` entries
        between printer.cfg and the KAMP cfgs. Klipper then halts at
        config parse with `gcode command _BMC_KAMP_INNER already
        registered` (key57) before our extras module even loads.

        Rather than trying to deduplicate in place (risky), we bail
        early and tell the user to --revert first. Revert restores
        pristine configs from backup, and a fresh install on a clean
        baseline is duplicate-free."""
        # Concatenate every cfg file under config/ for one big grep.
        # Tagged with ### <path> headers so the error message can point
        # at the offending file.
        cmd = (
            "for f in "
            "/mnt/UDISK/printer_data/config/printer.cfg "
            "/mnt/UDISK/printer_data/config/gcode_macro.cfg "
            "/mnt/UDISK/printer_data/config/KAMP/*.cfg "
            "; do "
            "[ -f \"$f\" ] && echo \"### FILE: $f\" && cat \"$f\"; "
            "done"
        )
        _, out, _ = self.run(cmd)

        # Pattern -> (display label, "singular" rule).
        # All expected counts are 1 when KAMP-K2 is cleanly installed.
        checks = [
            (r"^\s*\[include\s+KAMP/KAMP_Settings\.cfg\]",
             "[include KAMP/KAMP_Settings.cfg]"),
            (r"^\s*\[include\s+Adaptive_Meshing\.cfg\]",
             "[include Adaptive_Meshing.cfg]"),
            (r"^\s*\[include\s+Line_Purge\.cfg\]",
             "[include Line_Purge.cfg]"),
            (r"^\s*rename_existing\s*:\s*_BMC_KAMP_INNER",
             "rename_existing: _BMC_KAMP_INNER"),
            (r"^\[gcode_macro\s+BED_MESH_CALIBRATE\]\s*$",
             "[gcode_macro BED_MESH_CALIBRATE]"),
            (r"^\[restore_bed_mesh\]\s*$",
             "[restore_bed_mesh]"),
        ]

        dupes = []
        for pattern, label in checks:
            matches = re.findall(pattern, out, re.MULTILINE)
            if len(matches) > 1:
                dupes.append((label, len(matches)))

        if not dupes:
            self.log("no duplicate sections / includes found", "ok")
            return

        self.log("Config duplicates detected -- refusing to install:",
                 "err")
        for label, count in dupes:
            self.log(f"  - {label}: {count} occurrences (expected 1)",
                     "err")
        self.log("", "err")
        self.log("This usually means a previous install attempt left a "
                 "stale entry behind, often from repeated runs without "
                 "a clean revert in between.", "err")
        self.log("", "err")
        self.log("To fix: run this installer with --revert first to "
                 "restore the original Creality configs, then re-run "
                 "the installer. From the PowerShell one-liner, choose "
                 "option 2 (Revert) when prompted.", "err")
        self.log("", "err")
        self.log("If revert fails or the duplicates are older than the "
                 "oldest backup, you can open an issue with the output "
                 "of:  grep -rn 'rename_existing\\|_BMC_KAMP_INNER' "
                 "/mnt/UDISK/printer_data/config/", "err")
        sys.exit(1)

    def is_installed(self) -> bool:
        """Detect whether KAMP-K2 is already installed on the printer.

        Checks three independent markers so a half-install still reports
        as installed (better to treat a partial install as installed than
        as fresh — avoids double-patching configs).
        """
        markers = [
            "/usr/share/klipper/klippy/extras/restore_bed_mesh.py",
            "/mnt/UDISK/printer_data/config/KAMP/KAMP_Settings.cfg",
        ]
        for path in markers:
            if self.remote_exists(path):
                return True
        # Also check printer.cfg for our include line
        try:
            cfg = self.read_remote(PRINTER_CFG)
            if "[restore_bed_mesh]" in cfg or "KAMP/KAMP_Settings.cfg" in cfg:
                return True
        except FileNotFoundError:
            pass
        return False

    def detect(self) -> None:
        """Print machine-readable status and exit. Used by install.ps1 to
        decide whether to show the install or revert/update menu."""
        self.sanity_check()
        installed = self.is_installed()
        board = "unknown"
        try:
            board = self.detect_board()
        except Exception:
            pass
        print(f"KAMPK2_STATUS={'installed' if installed else 'fresh'}")
        print(f"KAMPK2_BOARD={board}")
        print(f"KAMPK2_HOST={self.host}")

    def detect_board(self) -> str:
        """Return "F008" (K2 Plus dual-Z) or "F021" (K2/Combo/Pro single-Z).

        Detection order:
          1. Honour --board override if not "auto".
          2. Parse the `# F008` / `# F021` header comment Creality emits at
             the top of printer.cfg.
          3. Fall back to structural markers: [stepper_z1] + [z_tilt] => F008.
          4. Default to F021 if ambiguous and log a warning.
        """
        if self.board != "auto":
            self.log(f"Board override: {self.board}", "ok")
            return self.board

        cfg = self.read_remote(PRINTER_CFG)
        header = cfg[:500]
        if re.search(r"^#\s*F008\b", header, re.MULTILINE):
            self.log("Board detected: F008 (K2 Plus, dual-Z)", "ok")
            return "F008"
        if re.search(r"^#\s*F021\b", header, re.MULTILINE):
            self.log("Board detected: F021 (K2 / K2 Combo / K2 Pro, single-Z)",
                     "ok")
            return "F021"
        has_z1 = re.search(r"^\[stepper_z1\]", cfg, re.MULTILINE) is not None
        has_tilt = re.search(r"^\[z_tilt\]", cfg, re.MULTILINE) is not None
        if has_z1 and has_tilt:
            self.log("Board detected via [stepper_z1]+[z_tilt]: F008 (K2 Plus)",
                     "ok")
            return "F008"
        self.log("Board could not be determined from printer.cfg header or "
                 "config sections. Defaulting to F021. Pass --board F008 if "
                 "this is a K2 Plus.", "warn")
        return "F021"

    # ---- install steps ----
    def backup_configs(self) -> None:
        # Prefer SSD if available (firmware-update survivable)
        ts = time.strftime("%Y%m%d_%H%M%S")
        # Use the same `kamp_k2_backup_<ts>` name on both paths. Earlier
        # revisions of this script used `kamp_k2_<ts>` (no "backup_" infix)
        # on the UDISK path, so on SSD-less K2 Plus printers backups created
        # by older installer versions are invisible to the `kamp_k2_backup_*`
        # glob that find_*_backup() uses. find_* now matches `kamp_k2_*`
        # (legacy-aware), but ALL NEW backups use the unified name.
        if "yes" in self.run("test -d /mnt/exUDISK && echo yes")[1]:
            base = f"/mnt/exUDISK/.system/kamp_k2_backup_{ts}"
        else:
            base = f"/mnt/UDISK/printer_data/config/backups/kamp_k2_backup_{ts}"
        self.log(f"Backing up current configs to {base}/", "step")
        if self.dry_run:
            self.log(f"[dry-run] would create {base} and copy configs", "dry")
            if self.local_backup_dir:
                self.log(f"[dry-run] would also mirror backup to "
                         f"{self.local_backup_dir}", "dry")
            return
        self.run(f"mkdir -p '{base}'")
        self.run(f"cp {PRINTER_CFG} '{base}/printer.cfg'")
        self.run(f"cp {GCODE_MACRO_CFG} '{base}/gcode_macro.cfg'")
        self.run(f"[ -d /mnt/UDISK/printer_data/config/KAMP ] && "
                 f"cp -r /mnt/UDISK/printer_data/config/KAMP '{base}/KAMP' "
                 f"|| true")
        self.log(f"On-printer backup saved: {base}", "ok")

        # Also mirror to the user's PC if a local backup dir was given.
        # Motivation: Creality firmware updates wipe /mnt/UDISK entirely and
        # can also wipe /mnt/exUDISK/.system in some variants. A copy on the
        # user's PC survives any printer-side wipe.
        if self.local_backup_dir:
            safe_host = re.sub(r"[^0-9A-Za-z_.-]", "_", self.host)
            local_dir = os.path.join(
                self.local_backup_dir, f"{safe_host}_{ts}")
            try:
                os.makedirs(local_dir, exist_ok=True)
                printer_cfg = self.read_remote(PRINTER_CFG)
                with open(os.path.join(local_dir, "printer.cfg"), "w",
                          encoding="utf-8", newline="") as f:
                    f.write(printer_cfg)
                gcode_cfg = self.read_remote(GCODE_MACRO_CFG)
                with open(os.path.join(local_dir, "gcode_macro.cfg"), "w",
                          encoding="utf-8", newline="") as f:
                    f.write(gcode_cfg)
                # Optionally mirror KAMP dir if it already exists (on re-install)
                rc, out, _ = self.run(
                    "test -d /mnt/UDISK/printer_data/config/KAMP && "
                    "ls /mnt/UDISK/printer_data/config/KAMP 2>/dev/null")
                if rc == 0 and out.strip():
                    kamp_local = os.path.join(local_dir, "KAMP")
                    os.makedirs(kamp_local, exist_ok=True)
                    for fname in out.strip().splitlines():
                        fname = fname.strip()
                        if not fname:
                            continue
                        try:
                            content = self.read_remote(
                                f"/mnt/UDISK/printer_data/config/KAMP/{fname}")
                            with open(os.path.join(kamp_local, fname), "w",
                                      encoding="utf-8", newline="") as f:
                                f.write(content)
                        except Exception as e:
                            self.log(f"  local backup: skipped "
                                     f"KAMP/{fname}: {e}", "warn")
                self.log(f"Local PC backup saved: {local_dir} "
                         "(survives printer firmware updates)", "ok")
            except Exception as e:
                self.log(f"Local backup failed: {e} (on-printer backup "
                         "still saved)", "warn")

    def copy_files(self) -> None:
        self.log("Copying KAMP-K2 files to printer...", "step")
        for local_rel, remote, mode in FILES_TO_COPY:
            local = os.path.join(REPO_ROOT, local_rel)
            if not os.path.isfile(local):
                self.log(f"  missing local file: {local}", "err")
                sys.exit(1)
            self.copy_file(local, remote, mode)
            self.log(f"  {local_rel} -> {remote}", "ok")

    def fix_adaptive_meshing_rename(self) -> None:
        """Patch KAMP's Adaptive_Meshing.cfg to use a unique rename target.

        Upstream KAMP uses `rename_existing: _BED_MESH_CALIBRATE`. On some
        Creality K2 firmware variants, `_BED_MESH_CALIBRATE` is already
        registered by `prtouch_v3_wrapper.so` at config load, which causes
        KAMP's rename to fail with `gcode command _BED_MESH_CALIBRATE already
        registered` when Klipper parses the macro section.

        Fix: rename KAMP's inner target to `_BMC_KAMP_INNER` — unlikely to
        collide with anything Creality registers. Also updates the call-
        through at the end of the macro. restore_bed_mesh.py knows to
        override this new name in KAMP mode.
        """
        path = "/mnt/UDISK/printer_data/config/KAMP/Adaptive_Meshing.cfg"
        src = self.read_remote(path)
        if "_BMC_KAMP_INNER" in src:
            self.log("Adaptive_Meshing.cfg: already patched, skipping", "ok")
            return
        new_src = src.replace(
            "rename_existing: _BED_MESH_CALIBRATE",
            "rename_existing: _BMC_KAMP_INNER",
        ).replace(
            "_BED_MESH_CALIBRATE mesh_min=",
            "_BMC_KAMP_INNER mesh_min=",
        )
        if new_src == src:
            self.log("Adaptive_Meshing.cfg: rename_existing target not found "
                     "(KAMP upstream format changed?)", "warn")
            return
        self.write_remote(path, new_src)
        self.log("Adaptive_Meshing.cfg: rename target -> _BMC_KAMP_INNER "
                 "(avoids collision with prtouch_v3)", "ok")

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

    def patch_forced_leveling_f008(self) -> None:
        """F008 (K2 Plus) only: flip `forced_leveling: true` -> `false`.

        Why: with forced_leveling=true, master-server sets the shared
        `bed_mesh_calibate_state` flag (visible in log format string
        `bed_mesh_calibate_state = %d, forced_leveling = %d` from
        Control/AppModeSdPrint.c:965) to a value that routes the wrapper's
        probe endstop into its own adaptive-region path. That path depends
        on state set up by the wrapper's own `cmd_BED_MESH_CALIBRATE`, which
        our `restore_bed_mesh.py` has unregistered in favour of upstream
        Klipper bed_mesh. Upstream bed_mesh calls `multi_probe_begin` on
        the endstop, endstop reads `bed_mesh_calibate_state`, finds it in a
        state expecting a gcode file that was never handed to upstream,
        raises PR_ERR_CODE_REGION_G29, Klipper shuts down.

        Flipping forced_leveling to false stops master-server from entering
        that branch at all. The wrapper's probe endstop takes the default
        (non-region) path, which upstream bed_mesh drives correctly.

        Safe because our override replaces Creality's adaptive flow entirely
        — there is nothing left that forced_leveling=true would enable that
        we still want.
        """
        cfg = self.read_remote(PRINTER_CFG)
        m = re.search(
            r"^(\s*)forced_leveling\s*:\s*true\b",
            cfg, re.MULTILINE | re.IGNORECASE,
        )
        if not m:
            self.log("forced_leveling: true not present (already false or "
                     "absent) — nothing to patch", "ok")
            return
        new_cfg = re.sub(
            r"^(\s*)forced_leveling\s*:\s*true\b",
            r"\1forced_leveling: false  # patched by KAMP-K2 "
            "(was true; conflicts with upstream bed_mesh override on F008)",
            cfg, count=1, flags=re.MULTILINE | re.IGNORECASE,
        )
        if new_cfg == cfg:
            self.log("forced_leveling patch produced no change (regex miss)",
                     "warn")
            return
        self.write_remote(PRINTER_CFG, new_cfg)
        self.log("printer.cfg: forced_leveling set to false for F008 "
                 "(was true; restored on revert via backup)", "ok")

    def patch_printer_cfg(self) -> None:
        """Ensure live (un-commented) [restore_bed_mesh] and
        [include KAMP/KAMP_Settings.cfg] sections exist in printer.cfg.

        Uses regex anchored to start-of-line so commented-out sections
        (e.g. `# [restore_bed_mesh] ...`) are treated as absent. A prior
        version used plain substring matching, which wrongly reported
        "already contains" when only a comment mentioning the section
        existed. That let the install complete while the module never
        actually loaded -- the user then hit the wrapper's IndexError
        because nothing had re-registered BED_MESH_CALIBRATE."""
        cfg = self.read_remote(PRINTER_CFG)
        has_restore = bool(re.search(
            r"^\s*\[restore_bed_mesh\]", cfg, re.MULTILINE))
        has_kamp_include = bool(re.search(
            r"^\s*\[include\s+KAMP/KAMP_Settings\.cfg\]",
            cfg, re.MULTILINE))
        changes = False
        if not has_kamp_include:
            # Prefer after [exclude_object] for clean placement
            anchor = "[exclude_object]"
            if anchor in cfg:
                snippet = f"{anchor}\n\n[restore_bed_mesh]\n\n" \
                          f"[include KAMP/KAMP_Settings.cfg]"
                cfg = cfg.replace(anchor, snippet, 1)
                changes = True
            else:
                cfg = cfg.rstrip() + PRINTER_CFG_SNIPPET + "\n"
                changes = True
        elif not has_restore:
            # KAMP include is live but restore_bed_mesh is absent or
            # commented out -- add a live [restore_bed_mesh] above it.
            cfg = re.sub(
                r"(^\s*\[include\s+KAMP/KAMP_Settings\.cfg\])",
                r"[restore_bed_mesh]\n\n\1",
                cfg, count=1, flags=re.MULTILINE)
            changes = True
        if changes:
            self.write_remote(PRINTER_CFG, cfg)
            self.log("printer.cfg: added/repaired "
                     "[restore_bed_mesh] + KAMP include", "ok")
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

        # 3. Insert KAMP mesh call + LINE_PURGE into START_PRINT if not
        #    already.
        #
        # To know whether the block is "already applied" we look at the
        # FUNCTIONAL evidence inside the [gcode_macro START_PRINT] body:
        #   - a bare `BED_MESH_CALIBRATE` line = mesh block is in place
        #   - a `LINE_PURGE` line = purge is in place
        # The earlier installer shipped a different comment marker
        # (`KAMP adaptive ...` rather than `KAMP-K2: adaptive ...`), so
        # string-matching on our current marker reported "missing" for
        # cfgs patched by the old version -- the installer then tried
        # to re-insert and hit "anchor not found" because the prior
        # patch had reshaped the surrounding text.
        start_print_match = re.search(
            r"^\[gcode_macro\s+START_PRINT\](.*?)(?=^\[)",
            cfg, re.MULTILINE | re.DOTALL)
        sp_body = start_print_match.group(1) if start_print_match else ""
        mesh_already = bool(
            re.search(r"^\s*BED_MESH_CALIBRATE\s*(#.*)?$",
                      sp_body, re.MULTILINE))
        purge_already = bool(
            re.search(r"^\s*LINE_PURGE\s*(#.*)?$",
                      sp_body, re.MULTILINE))

        if mesh_already:
            self.log("START_PRINT: BED_MESH_CALIBRATE call already "
                     "present, skipping mesh block insert", "ok")
        else:
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

        # LINE_PURGE intentionally NOT inserted into START_PRINT.
        #
        # Reported by Reddit user neturmel (issue #1): on CFS-equipped K2s,
        # the CFS pulls filament from a spool slot only when the slicer's
        # `T<n>` tool-select command runs. That `T<n>` is emitted by the
        # slicer AFTER START_PRINT returns. A LINE_PURGE inside START_PRINT
        # therefore fires with no filament at the nozzle -- result is an
        # empty purge, then the filament loads, then printing begins with
        # an un-purged nozzle (the exact symptom neturmel saw).
        #
        # Correct placement is in the SLICER's start-gcode, AFTER the
        # T<n> line -- see README "Slicer setup". We remove any old-style
        # in-macro LINE_PURGE here on re-install to keep things clean.
        if purge_already:
            self.log("START_PRINT: removing legacy LINE_PURGE call "
                     "(moved to slicer start-gcode; see README)",
                     "ok")
            # Remove the LINE_PURGE line AND the adjacent KAMP marker
            # comment if present (both forms: old "KAMP adaptive ..."
            # and current "KAMP-K2: adaptive ...").
            cfg = re.sub(
                r"^\s*#\s*KAMP(?:-K2)?\s*(?:adaptive\s*)?"
                r"(?:purge\s*line\s*(?:at\s*print[^\n]*)?)?\s*\n",
                "",
                cfg, flags=re.MULTILINE | re.IGNORECASE)
            cfg = re.sub(
                r"^\s*LINE_PURGE\s*(#.*)?\n",
                "",
                cfg, flags=re.MULTILINE)
        else:
            self.log("START_PRINT: no legacy LINE_PURGE to remove "
                     "(add LINE_PURGE to your slicer start-gcode "
                     "after the T<n> line; see README)", "ok")

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
    def _list_backup_dirs(self, base: str) -> list[str]:
        """List all backup dirs in `base`, newest first. Matches both
        the current `kamp_k2_backup_<ts>` and legacy `kamp_k2_<ts>`
        naming (older installer versions used the latter on UDISK-only
        printers -- see backup_configs for the history).

        Uses busybox-compatible commands: no -printf (not supported on
        K2 busybox find), no bash arrays. Two `ls -1dt` globs piped
        together give newest-first ordering by mtime; `awk '!seen[$0]++'
        ` deduplicates without changing order if the two globs
        somehow overlap.
        """
        rc, out, _ = self.run(
            f"(ls -1dt '{base}'/kamp_k2_backup_* 2>/dev/null; "
            f" ls -1dt '{base}'/kamp_k2_[0-9]* 2>/dev/null) "
            "| awk '!seen[$0]++'"
        )
        return [p.strip() for p in out.splitlines() if p.strip()]

    def find_latest_backup(self) -> str | None:
        """Return the path to the latest backup dir, preferring SSD."""
        for base in ["/mnt/exUDISK/.system",
                     "/mnt/UDISK/printer_data/config/backups"]:
            dirs = self._list_backup_dirs(base)
            if dirs:
                return dirs[0]
        return None

    def find_cleanest_backup(self) -> str | None:
        """Find the backup most likely to represent a pre-KAMP-K2 baseline.

        Each install creates a backup at install-time. If the user installed
        KAMP-K2 multiple times, later backups include the prior install's
        KAMP cfgs (and any duplicates that accumulated). We want the
        earliest backup whose `printer.cfg` has NO `[restore_bed_mesh]` and
        NO `[include KAMP/...]` -- that's the pristine Creality state.

        Falls back to the oldest backup if no fully-clean one is found.
        """
        candidates = []
        for base in ["/mnt/exUDISK/.system",
                     "/mnt/UDISK/printer_data/config/backups"]:
            candidates.extend(self._list_backup_dirs(base))
        if not candidates:
            return None
        # _list_backup_dirs returns newest first; sort oldest first for
        # the cleanest-baseline scan.
        candidates.sort()
        for path in candidates:
            pcfg_path = f"{path}/printer.cfg"
            try:
                pcfg = self.read_remote(pcfg_path)
            except FileNotFoundError:
                continue
            has_restore = bool(re.search(
                r"^\s*\[restore_bed_mesh\]", pcfg, re.MULTILINE))
            has_kamp = bool(re.search(
                r"^\s*\[include\s+KAMP/", pcfg, re.MULTILINE))
            if not has_restore and not has_kamp:
                return path
        # No pristine backup found; fall back to oldest available.
        return candidates[0]

    def find_local_backup(self) -> str | None:
        """Find the newest local-PC backup for this host in local_backup_dir."""
        if not self.local_backup_dir or not os.path.isdir(self.local_backup_dir):
            return None
        safe_host = re.sub(r"[^0-9A-Za-z_.-]", "_", self.host)
        candidates = []
        try:
            for name in os.listdir(self.local_backup_dir):
                full = os.path.join(self.local_backup_dir, name)
                if (os.path.isdir(full)
                        and name.startswith(f"{safe_host}_")
                        and os.path.isfile(os.path.join(full, "printer.cfg"))
                        and os.path.isfile(
                            os.path.join(full, "gcode_macro.cfg"))):
                    candidates.append((os.path.getmtime(full), full))
        except OSError:
            return None
        if not candidates:
            return None
        candidates.sort(reverse=True)
        return candidates[0][1]

    def clean_wipe(self) -> None:
        """Wipe the installed KAMP-K2 files and restore configs from the
        CLEANEST (pre-KAMP-K2) backup found on the printer. No klippy
        restart, no user prompts -- this is the programmatic step used
        before a clean-reinstall. Safer than the user-facing revert()
        because it picks the earliest pristine backup, not the latest
        (which on a multi-install system is contaminated)."""
        self.log("Searching for cleanest (pre-install) backup...", "step")
        backup = self.find_cleanest_backup()
        if not backup:
            self.log("No backups found; cannot auto-wipe. Manual revert "
                     "required.", "err")
            sys.exit(1)
        # Re-read it to confirm it's actually clean.
        try:
            pcfg = self.read_remote(f"{backup}/printer.cfg")
        except FileNotFoundError:
            self.log(f"Backup {backup} is missing printer.cfg", "err")
            sys.exit(1)
        is_pristine = not (
            re.search(r"^\s*\[restore_bed_mesh\]", pcfg, re.MULTILINE)
            or re.search(r"^\s*\[include\s+KAMP/", pcfg, re.MULTILINE)
        )
        if is_pristine:
            self.log(f"Cleanest backup: {backup} (pristine, pre-install)",
                     "ok")
        else:
            self.log(f"Cleanest backup: {backup} (WARNING: already "
                     "contains KAMP entries -- no fully pristine backup "
                     "found, using oldest available)", "warn")

        self.log(f"Restoring printer.cfg + gcode_macro.cfg from {backup}",
                 "step")
        self.run(f"cp '{backup}/printer.cfg' {PRINTER_CFG}")
        self.run(f"cp '{backup}/gcode_macro.cfg' {GCODE_MACRO_CFG}")

        self.log("Removing previous KAMP-K2 files (restore_bed_mesh.py + "
                 "KAMP/ dir)", "step")
        self.run("rm -f /usr/share/klipper/klippy/extras/restore_bed_mesh.py")
        self.run("rm -rf /mnt/UDISK/printer_data/config/KAMP")
        self.log("Wipe complete. Ready for fresh install.", "ok")

    def revert(self) -> None:
        """Restore the most recent backup and remove installed files.

        Order of preference: on-printer backup (SSD/UDISK) first, then
        local PC backup (--local-backup-dir). Firmware updates wipe
        the on-printer copies, so the local copy is the safety net."""
        self.log("=== Sanity checks ===", "step")
        self.sanity_check()

        self.log("=== Finding latest backup ===", "step")
        backup = self.find_latest_backup()
        local_backup = self.find_local_backup()
        use_local = False
        if not backup:
            self.log("No on-printer kamp_k2_backup_* directory found.", "warn")
            self.log("Checked /mnt/exUDISK/.system and "
                     "/mnt/UDISK/printer_data/config/backups", "warn")
            if local_backup:
                self.log(f"Found local PC backup: {local_backup}", "ok")
                self.log("Will restore from local PC backup (firmware update "
                         "likely wiped the on-printer backup).", "ok")
                backup = local_backup
                use_local = True
            else:
                self.log("No local PC backup either. Aborting.", "err")
                self.log("If you installed manually, restore your own backup "
                         "and remove: restore_bed_mesh.py, "
                         "[restore_bed_mesh], [include KAMP/...], "
                         "KAMP/ directory.", "err")
                sys.exit(1)
        else:
            self.log(f"Using on-printer backup: {backup}", "ok")
            if local_backup:
                self.log(f"(local PC backup also available: {local_backup})",
                         "info")

        # Confirm contents
        if use_local:
            files = os.listdir(backup)
            out = "\n".join(files)
        else:
            rc, out, _ = self.run(f"ls '{backup}'")
        self.log(f"Backup contents: {out.strip().replace(chr(10), ', ')}",
                 "info")
        if "printer.cfg" not in out or "gcode_macro.cfg" not in out:
            self.log("Backup is missing printer.cfg or gcode_macro.cfg — "
                     "aborting to avoid a half-revert.", "err")
            sys.exit(1)

        if self.dry_run:
            self.log(f"[dry-run] would restore from "
                     f"{'local PC' if use_local else 'on-printer'} "
                     f"backup at {backup}", "dry")
            self.log(f"[dry-run]   printer.cfg -> {PRINTER_CFG}", "dry")
            self.log(f"[dry-run]   gcode_macro.cfg -> {GCODE_MACRO_CFG}", "dry")
            self.log("[dry-run]   rm /usr/share/klipper/klippy/extras/"
                     "restore_bed_mesh.py", "dry")
            self.log("[dry-run]   rm -rf /mnt/UDISK/printer_data/config/KAMP",
                     "dry")
            self.log("[dry-run]   FIRMWARE_RESTART Klippy", "dry")
            return

        self.log("=== Restoring configs ===", "step")
        if use_local:
            # Push local files up to the printer
            with open(os.path.join(backup, "printer.cfg"), "r",
                      encoding="utf-8") as f:
                self.write_remote(PRINTER_CFG, f.read())
            with open(os.path.join(backup, "gcode_macro.cfg"), "r",
                      encoding="utf-8") as f:
                self.write_remote(GCODE_MACRO_CFG, f.read())
        else:
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
                         "to its pre-install state. Tries on-printer backup "
                         "first, falls back to --local-backup-dir if set "
                         "(useful after a firmware update wipes the printer).")
    ap.add_argument("--clean-reinstall", action="store_true",
                    help="Wipe existing KAMP-K2 files, restore configs from "
                         "the EARLIEST (pristine, pre-KAMP-K2) backup, then "
                         "do a full fresh install in one run. Use this if a "
                         "previous install accumulated duplicates or left "
                         "the printer in an inconsistent state.")
    ap.add_argument("--board", choices=["auto", "F008", "F021"], default="auto",
                    help="Board variant override. auto (default) detects from "
                         "printer.cfg. F008 = K2 Plus (dual-Z, needs "
                         "forced_leveling patch). F021 = K2 / K2 Combo / K2 "
                         "Pro (single-Z, no forced_leveling patch).")
    ap.add_argument("--detect", action="store_true",
                    help="Connect, report whether KAMP-K2 is already "
                         "installed, print machine-readable KAMPK2_STATUS="
                         "installed|fresh + KAMPK2_BOARD=... lines, and exit. "
                         "Used by install.ps1 to drive the install/update/"
                         "revert menu.")
    ap.add_argument("--local-backup-dir", default=None,
                    help="Also mirror the printer.cfg + gcode_macro.cfg "
                         "backup to this directory on the PC running the "
                         "installer. Firmware updates wipe printer-side "
                         "backups; this copy survives. --revert uses it as "
                         "a fallback when the printer has no backup.")
    args = ap.parse_args()

    inst = Installer(args.host, args.user, args.password,
                     dry_run=args.dry_run, verbose=args.verbose,
                     board=args.board,
                     local_backup_dir=args.local_backup_dir)
    try:
        inst.connect()

        if args.detect:
            inst.detect()
            return

        if args.revert:
            inst.revert()
            return

        if args.clean_reinstall:
            inst.log("=== CLEAN REINSTALL: wipe, then install fresh ===",
                     "step")
            inst.sanity_check()
            inst.clean_wipe()
            # Fall through into the normal install flow below.

        inst.log("=== Sanity checks ===", "step")
        inst.sanity_check()
        if not inst.exclude_object_section():
            inst.log("[exclude_object] not found in printer.cfg — KAMP "
                     "adaptive meshing needs it. Aborting.", "err")
            sys.exit(1)
        inst.log("=== Pre-install duplicate scan ===", "step")
        inst.check_for_duplicates()

        inst.log("=== Board detection ===", "step")
        board = inst.detect_board()

        inst.log("=== Backup ===", "step")
        inst.backup_configs()

        inst.log("=== File copy ===", "step")
        inst.copy_files()

        inst.log("=== Config patches ===", "step")
        inst.fix_kamp_settings()
        inst.fix_adaptive_meshing_rename()
        inst.patch_printer_cfg()
        if board == "F008":
            inst.patch_forced_leveling_f008()
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
