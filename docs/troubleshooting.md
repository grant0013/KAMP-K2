# Troubleshooting

Most issues encountered during the shakedown period are now either auto-fixed by the installer or covered below. If you hit something weird, the fastest recovery is almost always **option 3 — Clean reinstall** from the installer menu:

```powershell
iwr -useb https://raw.githubusercontent.com/grant0013/KAMP-K2/main/bootstrap.ps1 | iex
```

Pick `[3] Clean reinstall` at the prompt. It wipes any accumulated cruft, restores your printer.cfg/gcode_macro.cfg from the earliest pristine backup, and fresh-installs the current version in one step.

---

## Install-time issues

### "key57: gcode command _BMC_KAMP_INNER already registered"

Klipper halts at config parse with this error, printer unusable until you restart.

**What caused it** (fixed in commit [`0443e30`](https://github.com/grant0013/KAMP-K2/commit/0443e30) / release 1.0.0): a race condition between this project's override and KAMP's `rename_existing` both firing on `klippy:connect`. On some firmware builds (notably K2 Plus / F008) our handler ran first, pre-registered `_BMC_KAMP_INNER`, then KAMP's rename tried to create it and failed.

**Fix**: update to v1.0.0 or later (our override now registers on `klippy:ready` which fires *after* all connect handlers). If you're already on latest and still see it, you likely have duplicate entries in your configs — use **Clean reinstall** to clear them.

### "Config duplicates detected -- refusing to install"

Installer bails with a list of doubled-up sections. This is a safety check added in [`f10d7de`](https://github.com/grant0013/KAMP-K2/commit/f10d7de) to stop the installer from making things worse when previous attempts left stale entries behind.

**Fix**: use **Clean reinstall** from the menu. It restores your configs from the earliest pristine backup before installing.

### "IndexError: list index out of range" in prtouch_v3_wrapper.py:1922

K2 Plus only (F008). Happens when `_BMC_KAMP_INNER` is wired to the wrong handler and KAMP's internal call falls through to `prtouch_v3_wrapper.cmd_BED_MESH_CALIBRATE` without the params it expects.

**Fix**: this is the same root cause as the key57 race — update to v1.0.0+ where the override registers on both `_BMC_KAMP_INNER` and `_BED_MESH_CALIBRATE` names unconditionally, so whichever KAMP uses internally is always guarded. Clean reinstall if you're unsure.

---

## PowerShell installer issues (Windows)

### `iwr | iex` fails with "Unexpected attribute 'CmdletBinding'"

You're using an old one-liner that pointed at `install.ps1` directly. PowerShell's `Invoke-Expression` can't parse scripts that declare `[CmdletBinding()]` + `param()`.

**Fix**: use the bootstrap wrapper instead:

```powershell
iwr -useb https://raw.githubusercontent.com/grant0013/KAMP-K2/main/bootstrap.ps1 | iex
```

### Script shows `???#` or mangled characters

UTF-8 content served by GitHub, decoded as Windows cp1252 by PS 5.1.

**Fix**: already addressed — bootstrap.ps1 and install.ps1 ship as pure ASCII with UTF-8 BOM where needed. If you still see it, your one-liner is cached or pointing at an old copy. Close PowerShell, reopen, re-run.

### "KAMP-K2 install FAILED (exit code [long Python log] 0)"

Output capture bug in `Run-Installer`. Install actually succeeded; the wrapper misreported.

**Fix**: update to latest bootstrap ([`782bcac`](https://github.com/grant0013/KAMP-K2/commit/782bcac)). Run the one-liner again — if klippy is already in the correct state you'll be fine regardless of what the old wrapper said.

### PowerShell window closes before showing the result

Happens when `iwr | iex` evaluates bootstrap.ps1 in the current shell scope and an `exit` call propagates out and closes the terminal.

**Fix**: [`8a2b09f`](https://github.com/grant0013/KAMP-K2/commit/8a2b09f) removed the final `exit` and added a "Press Enter to close" pause. Re-run the one-liner.

### "install_k2.py: No such file or directory"

File ended up nested inside `C:\Users\<you>\KAMP-K2\KAMP-K2-main\` instead of directly in `C:\Users\<you>\KAMP-K2\`. Happens on machines where a previous install left a `backups/` directory that confused Download-Repo.

**Fix**: [`97aa662`](https://github.com/grant0013/KAMP-K2/commit/97aa662) reordered extraction. Delete the broken directory (`Remove-Item -Recurse C:\Users\$env:USERNAME\KAMP-K2`) and re-run the bootstrap.

### "Python 3 not found on PATH"

Installer now auto-installs Python via `winget` on Windows 10 1809+ and Windows 11. Just say "Y" when it asks.

**Fix**: if winget isn't available, install Python manually from [python.org/downloads](https://www.python.org/downloads/) and **tick "Add Python to PATH" on the first screen**.

### `.\install.ps1 -Host 192.168.x.x` fails with "A parameter cannot be found that matches parameter name 'Host'"

`-Host` is reserved (`$Host` is a built-in PowerShell automatic variable). Earlier docs said `-Host` incorrectly; the parameter name is `-PrinterHost`.

**Fix**:

```powershell
.\install.ps1 -PrinterHost 192.168.x.x
```

### "No backups found; cannot auto-wipe"

Only reported on SSD-less K2 Plus machines that had backups from before the naming was unified.

**Fix**: [`34456dc`](https://github.com/grant0013/KAMP-K2/commit/34456dc) made the backup discovery match both `kamp_k2_backup_<ts>` and legacy `kamp_k2_<ts>` patterns. Update to latest installer and try Clean reinstall again.

---

## Runtime / print issues

### "It's still doing a full mesh"

Expected signs that adaptive mesh **is** active (gcode console during a print):

```
// Algorithm: bicubic.
// Default probe count: 7,7.             ← your ceiling from [bed_mesh]
// Adapted probe count: 4,4.             ← smaller — this is the adaptive output
// Adapted mesh bounds: (x1, y1), (x2, y2).
// Happy KAMPing!
```

If the probe walks the full bed, one of these is usually wrong:

1. **`EXCLUDE_OBJECT_DEFINE` isn't in the gcode, or appears after `START_PRINT`.** Check the sliced gcode file directly:
   ```sh
   grep -nE "EXCLUDE_OBJECT_DEFINE|^START_PRINT" path/to/file.gcode | head
   ```
   `EXCLUDE_OBJECT_DEFINE` lines must appear **before** `START_PRINT`. OrcaSlicer: enable **Print Settings → Others → Output options → Label objects**.

2. **Override loaded in `direct mode` instead of `KAMP mode`.**
   ```sh
   ssh root@PRINTER tail -100 /mnt/UDISK/printer_data/logs/klippy.log | grep bed_mesh_override
   ```
   Correct output has `KAMP mode` and lists both registered names:
   ```
   bed_mesh_override: re-registered to guarded upstream on
   _BMC_KAMP_INNER, _BED_MESH_CALIBRATE (KAMP mode; ...)
   ```
   If you see `direct mode`, KAMP's macro isn't being loaded — check that `[include KAMP/KAMP_Settings.cfg]` is in `printer.cfg` and `[include Adaptive_Meshing.cfg]` is uncommented in `KAMP_Settings.cfg`.

3. **Creality Print is being used instead of Orca.** CP triggers Creality's own pre-print mesh in addition to KAMP's, so you get one full + one adaptive per print. The full one is not KAMP's doing. See the [Slicer compatibility](../README.md#slicer-compatibility) section in the README.

### "LINE_PURGE runs with no filament — empty purge line, then un-purged first layer" (CFS users)

Reported as [issue #1](https://github.com/grant0013/KAMP-K2/issues/1). Older installer versions inserted `LINE_PURGE` into `START_PRINT` — but on CFS-equipped printers the CFS only pulls filament to the nozzle when the slicer emits `T<n>` (tool select), which runs **after** `START_PRINT` returns. So `LINE_PURGE` fires with an empty extruder.

**Fix**: [`309a474`](https://github.com/grant0013/KAMP-K2/commit/309a474) removed `LINE_PURGE` from `START_PRINT` and moved it to the slicer start-gcode where it belongs. Update the installer (it'll auto-strip the old line), then put `LINE_PURGE` between `T[initial_no_support_extruder]` and `M204 S2000` in your slicer's start-gcode — see [README Slicer setup](../README.md#slicer-setup) for the full 5-line form.

### "LINE_PURGE runs at (0, 0) or an odd location"

KAMP computes purge start from the object bounding box. `(0, 0)` means `printer.exclude_object.objects` was empty when `LINE_PURGE` ran — same root cause as the mesh going full-bed.

**Fix**: same as mesh troubleshoot above — make sure `EXCLUDE_OBJECT_DEFINE` is in the gcode before `START_PRINT`.

### "Print cancels with 'BED_MESH_CALIBRATE fail'"

Master-server watches for `[G29_TIME]Execution time:` in gcode responses as its "mesh complete" signal. If that exact string doesn't appear, it treats the mesh as failed and may cancel.

**Check**: your hijacked `G29` and `BED_MESH_CALIBRATE_START_PRINT` macros both emit:
```
M118 [G29_TIME]Execution time: 0.0 seconds, Time spent at each point: 0.0
```
If you removed or edited that line, put it back. The literal substring `[G29_TIME]Execution time:` is what master-server greps for.

### "IndexError on startup" (klippy won't connect)

If klippy fails to start with a traceback ending in `IndexError: list index out of range` at `bed_mesh.py`:

```
ConfigError: ... IndexError: list index out of range
```

...it usually means you named the config section `[bed_mesh_override]` or `[bed_mesh_something]`. Klipper's `ProfileManager` iterates `get_prefix_sections('bed_mesh')` and chokes on anything starting with `bed_mesh`.

**Fix**: section name **must** be `[restore_bed_mesh]` (doesn't start with `bed_mesh`). The installer writes this correctly; check only if you hand-edited.

### "Include file 'KAMP/Adaptive_Meshing.cfg' does not exist"

KAMP_Settings.cfg's stock include paths use `./KAMP/Adaptive_Meshing.cfg`, relative to the top-level config directory. When the file is inside a `KAMP/` subdirectory, Klipper resolves includes relative to the including file — path becomes `config/KAMP/KAMP/Adaptive_Meshing.cfg`, which doesn't exist.

**Fix**: in `/mnt/UDISK/printer_data/config/KAMP/KAMP_Settings.cfg`, change `[include ./KAMP/Adaptive_Meshing.cfg]` to `[include Adaptive_Meshing.cfg]` (same for Line_Purge). The installer does this automatically.

### "Installer warns 'Override log message not found' but prints Done"

False negative — the installer's log-grep runs slightly before the log line flushes to disk in some cases.

**Fix**: verify manually:
```sh
ssh root@PRINTER tail -100 /mnt/UDISK/printer_data/logs/klippy.log | grep bed_mesh_override
```
If you see `re-registered to guarded upstream on _BMC_KAMP_INNER, _BED_MESH_CALIBRATE (KAMP mode; ...)`, you're fine. If nothing matches, FIRMWARE_RESTART and check again.

### "START_PRINT: anchor not found, skipping mesh block insert"

Older installer versions checked for a specific comment marker to decide if `START_PRINT` was already patched. If a prior install used a different marker string, the check returned "missing" and the installer then failed to find its insertion anchor because the prior patch had reshaped the surrounding text.

**Fix**: [`17f4ad9`](https://github.com/grant0013/KAMP-K2/commit/17f4ad9) switched to functional-marker detection (looks for bare `BED_MESH_CALIBRATE` and `LINE_PURGE` calls inside the START_PRINT body). Update the installer — you'll now see "BED_MESH_CALIBRATE call already present, skipping" instead.

---

## Slicer-related issues

### Creality Print shows double mesh

Creality Print triggers Creality's pre-print calibration flow **in addition** to KAMP's adaptive mesh — you get one full mesh (Creality's) + one adaptive mesh (KAMP's) per print. This is CP behaviour, not a bug in KAMP-K2.

**Fix**: switch to OrcaSlicer. KAMP-K2 is developed and tested against Orca; CP is partially compatible but not recommended. See [Slicer compatibility](../README.md#slicer-compatibility) in the README.

### Chamber light stays off during mesh + purge

Creality's recent firmware versions gate the chamber LED on `print_stats.state == "printing"`, which doesn't fire until the first real extrusion in the gcode body. Orca-uploaded prints skip Creality's in-between states where the LED would normally come on.

**Fix** (optional): add `SET_PIN PIN=LED VALUE=1` at the top of `[gcode_macro START_PRINT]` body. That explicitly forces the LED on at print start, overriding master-server's gate. Not in the installer by default — purely cosmetic.

### "Probe count X,X exceeds allowed maximum"

Creality's firmware caps the bed mesh `probe_count` at a per-version maximum (1.1.3.x permits up to 21×21; 1.1.4.x caps lower around 13×13). The cap is enforced by master-server's config validator, not Klipper itself.

**Fix**: set a value at or below your firmware's cap. KAMP scales from whatever ceiling you set, so smaller doesn't hurt adaptive behaviour — see [docs/tuning.md](tuning.md) for guidance.

---

## Recovery

### Clean reinstall (recommended for any weird state)

```powershell
iwr -useb https://raw.githubusercontent.com/grant0013/KAMP-K2/main/bootstrap.ps1 | iex
```

Menu → `[3] Clean reinstall`. Wipes KAMP-K2 files, restores your configs from the earliest pristine backup found on the printer, then installs fresh in one run.

### Full revert to stock

```powershell
iwr -useb https://raw.githubusercontent.com/grant0013/KAMP-K2/main/bootstrap.ps1 | iex
```

Menu → `[2] Revert`. Restores configs + removes KAMP-K2 files. Uses the latest backup by default; falls back to local PC backup (saved at `%USERPROFILE%\KAMP-K2\backups\`) if the on-printer backup is missing (e.g. wiped by firmware update).

### Manual recovery (klippy halted, can't run installer remotely)

SSH into the printer and run:

```sh
# pick the oldest backup — likely cleanest (pre-KAMP-K2 baseline)
ls -1rt /mnt/UDISK/printer_data/config/backups/kamp_k2_* | head -1
# or /mnt/exUDISK/.system/kamp_k2_* if you have an SSD
BACKUP=/mnt/UDISK/printer_data/config/backups/kamp_k2_<earliest_timestamp>
cp "$BACKUP/printer.cfg" /mnt/UDISK/printer_data/config/
cp "$BACKUP/gcode_macro.cfg" /mnt/UDISK/printer_data/config/
rm -f /usr/share/klipper/klippy/extras/restore_bed_mesh.py
rm -rf /mnt/UDISK/printer_data/config/KAMP
/etc/init.d/klipper restart
```

### After a Creality firmware update

Creality OTA wipes `/mnt/UDISK` and sometimes `/mnt/exUDISK/.system` too. KAMP-K2 files and any on-printer backups may both be gone.

**Fix**: the installer also saves a **local PC backup** at `%USERPROFILE%\KAMP-K2\backups\` that survives any printer-side wipe. Just re-run the one-liner — Revert will automatically fall back to the local backup if the on-printer one is missing.

---

## Getting help

Open an issue at [github.com/grant0013/KAMP-K2/issues](https://github.com/grant0013/KAMP-K2/issues) with:

- Printer model (K2 / K2 Plus / K2 Combo / K2 Pro)
- Creality firmware version (touchscreen → Settings → About)
- What the installer output looked like (full terminal paste)
- `tail -300 /mnt/UDISK/printer_data/logs/klippy.log`
- `grep bed_mesh_override /mnt/UDISK/printer_data/logs/klippy.log | tail -5`
- For adaptive-mesh questions, the first 200 lines of your sliced gcode file (shows EXCLUDE_OBJECT_DEFINE placement)

Scrub anything personal (network names, custom macros unrelated to the issue) before pasting.
