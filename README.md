# KAMP-K2

[![Release](https://img.shields.io/github/v/release/grant0013/KAMP-K2?display_name=tag)](https://github.com/grant0013/KAMP-K2/releases)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Klipper compatible](https://img.shields.io/badge/Klipper-compatible-5a9e5a.svg)](https://www.klipper3d.org/)
[![Tested on K2](https://img.shields.io/badge/tested-K2%20%2B%20K2%20Plus-00a1ff.svg)](#compatibility)
[![Issues welcome](https://img.shields.io/github/issues/grant0013/KAMP-K2.svg)](https://github.com/grant0013/KAMP-K2/issues)

**KAMP (Klipper Adaptive Meshing & Purging) — Creality K2 fork**

A fork of [kyleisah/Klipper-Adaptive-Meshing-Purging](https://github.com/kyleisah/Klipper-Adaptive-Meshing-Purging) with the small amount of extra glue Creality K2 printers need to make KAMP actually work.

> Looking for upstream KAMP's own readme? It's preserved as [`README_KAMP_UPSTREAM.md`](README_KAMP_UPSTREAM.md).

## What's different from upstream KAMP

Creality's K2 firmware does two things that break upstream KAMP out of the box (on the `CR0CN200400C10` board — K2, K2 Combo, K2 Pro). K2 Plus has a half-implemented Creality adaptive mesh on a different board but users report it's unreliable; this fork replaces that too.

1. **`prtouch_v3_wrapper.so` hijacks `BED_MESH_CALIBRATE`** with a non-adaptive implementation that ignores `MESH_MIN` / `MESH_MAX` / `PROBE_COUNT` and crashes with `IndexError` when those are passed. KAMP relies on the upstream Klipper handler being present, so KAMP calls fail silently or blow up.
2. **`master-server` daemon independently fires `G29` and `BED_MESH_CALIBRATE_START_PRINT`** during print prep, outside of any slicer start-gcode. Those fire before KAMP can run and triggers a stock full-bed mesh every time.

KAMP-K2 fixes both:

- **`extras/restore_bed_mesh.py`** — a small Klipper extras module that re-registers the upstream `bed_mesh.BedMeshCalibrate.cmd_BED_MESH_CALIBRATE` handler, wrapping it with a guard that requires `MESH_MIN` / `MESH_MAX` bounds to run. Bare calls (from master-server) are no-ops. KAMP-aware: detects KAMP's `rename_existing: _BED_MESH_CALIBRATE` and overrides the inner handler so KAMP stays the user-facing entry point.
- **Macro hijacks**: `G29` and `BED_MESH_CALIBRATE_START_PRINT` are replaced with no-op macros that emit a fake `[G29_TIME]Execution time: 0.0` response so master-server's print-prep sequence is satisfied without actually running a mesh. The real mesh runs inside `START_PRINT` where KAMP has slicer metadata available.
- **`START_PRINT` patches**: a call to bare `BED_MESH_CALIBRATE` (which KAMP wraps with adaptive bounds) and a `LINE_PURGE` call are inserted in the right places relative to the K2's CFS-specific nozzle clean and prime moves.

Upstream KAMP's `Adaptive_Meshing.cfg`, `Line_Purge.cfg`, and `KAMP_Settings.cfg` are included unchanged — this fork only *adds* files, never modifies KAMP's own behaviour.

## Quick start

### Windows (one-liner, no git needed)

Open **PowerShell** (not cmd.exe) and paste:

```powershell
iwr -useb https://raw.githubusercontent.com/grant0013/KAMP-K2/main/bootstrap.ps1 | iex
```

The script checks for Python (prompts to install if missing), downloads the repo, installs `paramiko`, asks for your printer's IP, and runs the installer. No manual SSH required.

### macOS / Linux / manual

```sh
git clone https://github.com/grant0013/KAMP-K2
cd KAMP-K2
pip install paramiko
python install_k2.py --host 192.168.x.x
```

Replace `192.168.x.x` with your printer's IP. The installer uses the Creality stock root password (`creality_2024`) by default; override with `--password MYPASS` if yours has been changed.

See [`docs/INSTALL_K2.md`](docs/INSTALL_K2.md) for the step-by-step the installer performs (useful if you want to do it manually, or understand what's being changed).

## Compatibility

Two different boards in the K2 family behave differently:

| Printer | Board | Stock adaptive mesh? | KAMP-K2 needed? |
|---|---|---|---|
| **K2 / K2 Combo / K2 Pro** | `CR0CN200400C10` (F021) | **No** (no `forced_leveling` toggle in printer.cfg, wrapper doesn't support adaptive bounds) | **Yes — this is the only option** |
| **K2 Plus** | `CR0CN240319C13` (F008) | Yes, but **off by default** and forum-reported unreliable | Optional alternative; more maintainable than Creality's flaky toggle |
| K1 / K1C / K1 Max | CR4CU220812S* | No | Should work (same `prtouch_v3_wrapper` + master-server architecture); untested — please file an issue if you try it |
| K1 SE | CR4CU220812S12 | No | Unknown (wrapper may be `prtouch_v2`; minor path change in installer probably needed) |
| Non-Creality Klipper printer | — | — | **Don't use this fork** — use [upstream KAMP](https://github.com/kyleisah/Klipper-Adaptive-Meshing-Purging) directly; you don't have a wrapper to bypass |

**Adaptive line purging (`LINE_PURGE`) is *not* available in any Creality K-series stock firmware.** Even K2 Plus owners with Creality's built-in adaptive mesh working get no purge-line adaptation out of the box. KAMP-K2 brings both features on every variant it supports.

Tested on my K2 Combo (`CR0CN200400C10`, firmware `V1.1.4.1`, Klipper `09faed31-dirty`).

## Slicer setup

### OrcaSlicer

**Printer Settings → Machine G-code → Machine start G-code** — replace the default with exactly these five lines:

```
START_PRINT EXTRUDER_TEMP=[nozzle_temperature_initial_layer] BED_TEMP=[bed_temperature_initial_layer_single]
T[initial_no_support_extruder]
LINE_PURGE
M204 S2000
M83
```

> **Why these exact lines, in this order**:
> - `START_PRINT` runs heating, homing, adaptive mesh, nozzle clean. Does **not** load filament (that's slicer-side on K2).
> - `T[initial_no_support_extruder]` triggers the CFS (or direct-drive loader) to actually pull filament to the nozzle.
> - `LINE_PURGE` **must come after T<n>** — if it runs before, there's no filament at the nozzle yet and you get an empty purge followed by an un-purged start (reported by [issue #1](https://github.com/grant0013/KAMP-K2/issues/1)).
> - `M204 S2000` + `M83` — accel limit + relative extrusion. Anything beyond these is usually the slicer's default fixed purge line, which wastes filament now that KAMP is doing an adaptive one.

**Print Settings → Others → Bed mesh** (match your `[bed_mesh]` section in printer.cfg):
- Bed mesh min: `5, 5`
- Bed mesh max: `255, 255` on K2/K2 Combo (260³), or `345, 345` on K2 Plus (350³) — read the `mesh_max:` line in your printer.cfg to be sure
- Probe point distance: `50, 50`
- Mesh margin: `5`

**Print Settings → Others → Output options**: make sure **Label objects** is enabled (this writes the `EXCLUDE_OBJECT_DEFINE` lines that KAMP reads).

**You do NOT need to pass `MESH_MIN` / `MESH_MAX` / `PROBE_COUNT`** — KAMP figures them out from the loaded gcode's `exclude_object` metadata.

To skip the adaptive mesh entirely on a specific slice (e.g. a calibration cube you don't care about), add `MESH=0` to the `START_PRINT` call:

```
START_PRINT EXTRUDER_TEMP=[...] BED_TEMP=[...] MESH=0
```

### Other slicers

Any slicer that supports `[exclude_object]` / object-labeled gcode output will work. PrusaSlicer and SuperSlicer both do. The only requirement is that the `EXCLUDE_OBJECT_DEFINE` lines appear in the file **before** the `START_PRINT` call, so `printer.exclude_object.objects` is populated by the time KAMP reads it. OrcaSlicer does this by default; other slicers vary.

## Verifying it worked

After running the installer, open your printer's gcode console and slice a small test print. You should see something like:

```
// Algorithm: bicubic.
// Default probe count: 11,11.
// Adapted probe count: 4,4.
// Default mesh bounds: (5, 5), (255, 255).
// Adapted mesh bounds: (106.0, 39.0), (154.0, 221.0).
// KAMP adjustments successful. Happy KAMPing!
// KAMP purge starting at 115.0, 29.0 and purging 30.0mm of filament, requested flow rate is 12.0mm3/s.
```

The probe should then walk only the area covering your printed objects.

In `klippy.log`, the override should announce itself on every Klippy restart:

```
[INFO] bed_mesh_override: _BED_MESH_CALIBRATE re-registered to guarded upstream
       (KAMP mode; bare calls are no-ops; MESH_MIN/MAX required to run)
```

## What about Smart_Park?

KAMP-K2 **does not enable** upstream KAMP's `Smart_Park.cfg`. The K2 has its own `BOX_GO_TO_EXTRUDE_POS` macro (part of the CFS filament-change flow) that already parks the nozzle at a reasonable location during print prep. Adding Smart_Park on top would just override that with a parking location near the first layer — which the printer was about to move to anyway. If you have a strong reason to enable it, uncomment the line in `KAMP_Settings.cfg` by hand after install.

## Reverting

Run the installer with `--revert` (not yet implemented — see Issues) or restore the backup that the installer makes at:

- `/mnt/exUDISK/.system/kamp_k2_backup_<timestamp>/` (on printers with the external SSD), or
- `/mnt/UDISK/printer_data/config/backups/kamp_k2_<timestamp>/` (without SSD)

Then remove `extras/restore_bed_mesh.py`, remove `[restore_bed_mesh]` and `[include KAMP/KAMP_Settings.cfg]` from `printer.cfg`, and restart Klippy.

## Tuning

If you want adaptive meshing to run fewer probe points on small parts (faster mesh time, same first-layer quality), see [`docs/tuning.md`](docs/tuning.md). Default ceiling on most K2s is `probe_count: 11,11` — the doc explains when and why dropping it to `7,7` (Creality's own default) or lower is worth doing, and when it isn't.

## How it works (deeper)

- [`docs/how-it-works.md`](docs/how-it-works.md) — the full hijack stack and why each piece is needed
- [`docs/tuning.md`](docs/tuning.md) — optional `probe_count` tuning and `MESH=0` skip-mesh flag

## Credits

Built on top of:
- [Klipper](https://www.klipper3d.org/) — Kevin O'Connor and contributors
- [kyleisah/Klipper-Adaptive-Meshing-Purging](https://github.com/kyleisah/Klipper-Adaptive-Meshing-Purging) — the upstream KAMP project, unchanged in this fork
- [pellcorp/creality](https://github.com/pellcorp/creality) — early K1 / K2 reverse-engineering groundwork

## Licence

GPL v3, matching upstream KAMP and Klipper. See [`LICENSE.md`](LICENSE.md).
