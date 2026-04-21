# Manual install — what `install_k2.py` does, step by step

If you want to understand what the installer is doing, or install by hand, the nine steps below replicate it exactly. If you just want to get going, run `python install_k2.py --host 192.168.x.x` and skip this doc.

## Prerequisites

- SSH access to the printer as `root`. Stock Creality root password is `creality_2024`. If yours has been rotated by a firmware update, find the current one — look for it on the touchscreen recovery menu or ask Creality support.
- `[exclude_object]` already present in `/mnt/UDISK/printer_data/config/printer.cfg`. K2 stock has this. If yours doesn't, add `[exclude_object]` as its own section.
- Your slicer must be set up to emit `EXCLUDE_OBJECT_DEFINE` lines **before** `START_PRINT` in the gcode output. OrcaSlicer with "Label objects" enabled does this by default.

## Step 1 — Back up your configs

```sh
ssh root@PRINTER_IP
BACKUP=/mnt/exUDISK/.system/kamp_k2_backup_$(date +%Y%m%d_%H%M%S)
# fall back to UDISK if no external SSD:
[ -d /mnt/exUDISK ] || BACKUP=/mnt/UDISK/printer_data/config/backups/kamp_k2_$(date +%Y%m%d_%H%M%S)
mkdir -p "$BACKUP"
cp /mnt/UDISK/printer_data/config/printer.cfg "$BACKUP/"
cp /mnt/UDISK/printer_data/config/gcode_macro.cfg "$BACKUP/"
```

## Step 2 — Copy `restore_bed_mesh.py` to Klipper extras

From your cloned KAMP-K2 repo:

```sh
scp extras/restore_bed_mesh.py root@PRINTER_IP:/usr/share/klipper/klippy/extras/
```

Verify:

```sh
ssh root@PRINTER_IP 'python3 -c "import ast; \
  ast.parse(open(\"/usr/share/klipper/klippy/extras/restore_bed_mesh.py\").read()); \
  print(\"parse OK\")"'
```

## Step 3 — Copy KAMP config files

```sh
ssh root@PRINTER_IP 'mkdir -p /mnt/UDISK/printer_data/config/KAMP'
scp Configuration/KAMP_Settings.cfg root@PRINTER_IP:/mnt/UDISK/printer_data/config/KAMP/
scp Configuration/Adaptive_Meshing.cfg root@PRINTER_IP:/mnt/UDISK/printer_data/config/KAMP/
scp Configuration/Line_Purge.cfg root@PRINTER_IP:/mnt/UDISK/printer_data/config/KAMP/
```

## Step 4 — Fix `KAMP_Settings.cfg` include paths

The distributed KAMP_Settings uses `./KAMP/Adaptive_Meshing.cfg` paths that are relative to the **top-level config dir**. When KAMP_Settings.cfg is placed inside a `KAMP/` subdirectory, Klipper resolves includes relative to the including file, so `./KAMP/...` becomes `KAMP/KAMP/...` which doesn't exist.

Fix: in `/mnt/UDISK/printer_data/config/KAMP/KAMP_Settings.cfg`, change:

```
#[include ./KAMP/Adaptive_Meshing.cfg]
#[include ./KAMP/Line_Purge.cfg]
```

to (uncommented and path-stripped):

```
[include Adaptive_Meshing.cfg]
[include Line_Purge.cfg]
```

## Step 5 — Add includes to `printer.cfg`

Edit `/mnt/UDISK/printer_data/config/printer.cfg` and add these two lines. A clean spot is right after `[exclude_object]`:

```
[exclude_object]

[restore_bed_mesh]

[include KAMP/KAMP_Settings.cfg]
```

The section name `[restore_bed_mesh]` is **important** — Klipper's `ProfileManager` iterates `get_prefix_sections('bed_mesh')` and crashes with `IndexError` if it finds a section like `[bed_mesh_override]`. `restore_bed_mesh` doesn't start with `bed_mesh`, so it's safe.

## Step 6 — Hijack `G29` macro

In `/mnt/UDISK/printer_data/config/gcode_macro.cfg`, find the existing `[gcode_macro G29]` block and **replace it entirely** with:

```
[gcode_macro G29]
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
```

The `M118 [G29_TIME]...` line is critical — master-server scans responses for that exact string to determine mesh completion. Without it, master-server thinks the mesh failed and pauses the print.

## Step 7 — Hijack `BED_MESH_CALIBRATE_START_PRINT` macro

Same file, find the existing block and replace with:

```
[gcode_macro BED_MESH_CALIBRATE_START_PRINT]
description: Hijacked by KAMP-K2 - defers mesh to START_PRINT (adaptive). Emits fake [G29_TIME] so master-server is satisfied.
gcode:
  BED_MESH_CLEAR
  M118 BED_MESH_CALIBRATE_START_PRINT deferred (KAMP adaptive in START_PRINT)
  M118 [G29_TIME]Execution time: 0.0 seconds, Time spent at each point: 0.0
```

## Step 8 — Insert KAMP-triggering calls into `START_PRINT`

Find `[gcode_macro START_PRINT]`. Inside its `gcode:` body, locate the `{% endif %}` that closes the `prepare == 0 / prepare == 1` if/else. Right after that `{% endif %}`, before the next `M140 S{params.BED_TEMP}`, insert:

```
  # KAMP-K2: adaptive mesh. BED_MESH_CALIBRATE is wrapped by KAMP and
  # reads exclude_object metadata from the loaded gcode to size the probe
  # to just the print area.
  {% if params.MESH|default(1)|int == 1 %}
    BED_MESH_CLEAR
    BED_MESH_CALIBRATE
  {% else %}
    M118 Mesh skipped (MESH=0 from slicer)
  {% endif %}
```

Then find the line `G92 E0 ; Reset Extruder` later in the same macro. Immediately before it (after the last `BOX_NOZZLE_CLEAN`), insert:

```
  # KAMP-K2: adaptive purge line at print-area edge
  LINE_PURGE
```

## Step 9 — Restart Klippy and verify

```sh
ssh root@PRINTER_IP '/etc/init.d/klipper restart'
# wait ~10 seconds
ssh root@PRINTER_IP 'tail -50 /mnt/UDISK/printer_data/logs/klippy.log | grep bed_mesh_override'
```

You should see two lines:

```
[INFO] bed_mesh_override: upstream bound to BedMeshCalibrate via class BedMeshCalibrate
       (bypasses any subclass override)
[INFO] bed_mesh_override: re-registered to guarded upstream on _BMC_KAMP_INNER,
       _BED_MESH_CALIBRATE (KAMP mode; bare calls are no-ops; MESH_MIN/MAX required to run)
```

The words `KAMP mode` and the two names (`_BMC_KAMP_INNER, _BED_MESH_CALIBRATE`) are what you're looking for — they confirm the override guarded both possible KAMP-internal targets. Either of those names gets called depending on KAMP config version; having both covered means KAMP's actual call always lands on our guarded upstream.

If you see `direct mode` instead, KAMP isn't being loaded — check that `[include KAMP/KAMP_Settings.cfg]` is in `printer.cfg` and that `[include Adaptive_Meshing.cfg]` is uncommented in `KAMP_Settings.cfg`.

## Step 10 — Slicer start-gcode

KAMP's `LINE_PURGE` emits its own adaptive purge line at the edge of the print area. Most slicers' default start-gcode also runs a purge line of their own (front corner, fixed position). You end up with two purges: one adaptive, one not. Cosmetic rather than broken, but the extra material is wasteful.

Replace your slicer's default start-gcode with just the essentials. For **OrcaSlicer / Bambu Studio** (`Printer settings → Machine G-code → Machine start G-code`):

```
START_PRINT EXTRUDER_TEMP=[nozzle_temperature_initial_layer] BED_TEMP=[bed_temperature_initial_layer_single]
T[initial_no_support_extruder]
M204 S2000
M83
```

That's it — four lines. The K2's own `START_PRINT` macro (now KAMP-aware) handles the rest: heating, homing, mesh, nozzle clean, and the adaptive `LINE_PURGE`. Everything else the default start-gcode was doing is now redundant or conflicting.

For **PrusaSlicer / SuperSlicer**, the `[nozzle_temperature_initial_layer]` and `[bed_temperature_initial_layer_single]` variable names differ — use `[first_layer_temperature]` and `[first_layer_bed_temperature]` respectively.

## Done

Slice a print with EXCLUDE_OBJECT_DEFINE enabled in your slicer output, and watch the gcode console during start:

```
// Algorithm: bicubic.
// Default probe count: 11,11.
// Adapted probe count: 4,4.
// Adapted mesh bounds: (106.0, 39.0), (154.0, 221.0).
// Happy KAMPing!
// KAMP purge starting at 115.0, 29.0 ...
```

If you see this, you're done.
