# checks: automatic verification of posted CNC milling programs

Checks for AI-generated 3-axis programs posted for a Haas VF-2 (Fanuc-style G-code, G20 or G21).
They need no Fusion. Two checks run on a job folder:

- **nc_lint** reads the G-code without geometry. It checks the safe start, units, work offset, T/M06/G43 H,
  spindle, coolant, feed and rpm limits, canned cycles, arcs, rapids below the clearance plane over the stock,
  and the program end.
- **stock_sim** runs a Z-map (height-field) material removal of the posted G-code. It sweeps the real tool
  shapes (flat, ball, bull nose, drill/spot/chamfer cone) and compares the result with the part STEP
  seen from the top of each setup. It reports gouge and leftover maps, rapids through stock, holder
  collisions, cuts deeper than the flute length, and per-tool air cutting. Stock carries over between
  setups (top and flip). A final leftover check is done in the part frame after the last setup.

## Run

```sh
# from the project root (cad-cam-pion/)
.venv/bin/python -m checks run path/to/job              # writes path/to/job/check_out/report.{json,md}
.venv/bin/python -m checks run path/to/job --png        # + gouge/leftover map images per setup
.venv/bin/python -m checks run path/to/job --res 0.005  # finer Z-map (job units)
.venv/bin/python -m checks run path/to/job --no-sim     # lint only
.venv/bin/python -m checks lint prog.nc --units inch    # lint one file, no job (skips stock rules)
.venv/bin/python -m pytest checks/tests -q              # self-test (~7 s)
.venv/bin/python -m checks.tests.synthetic /tmp/demo two_setup   # write an example job folder
```

The exit code is 0 on pass and 1 on fail. `report.md` is the short summary. `report.json` holds every
finding with file:line, the worst NC lines per problem, and the regions (max, area, worst XY).

## Job folder

```
job/
  job.json      # below
  tools.json    # tool list
  part.step     # or .stl/.obj (set part_units)
  1001.nc ...   # posted programs, listed per setup in machining order
```

`job.json` (lengths in `units`):

```json
{
  "units": "inch",
  "part": "part.step",
  "tools": "tools.json",
  "stock": {"min": [0, 0, -0.1], "max": [6, 4, 1.05]},
  "setups": [
    {"name": "OP1", "wcs": "G54", "nc": ["1001.nc"],
     "rotation_deg": [0, 0, 0], "translation": [0, 0, -1], "min_tool_z": -1.15},
    {"name": "OP2", "wcs": "G55", "nc": ["1002.nc"],
     "rotation_deg": [180, 0, 0], "translation": [0, 4, 0]}
  ],
  "machine":    {"max_feed": 650, "max_rapid": 1000, "max_rpm": 8100},
  "tolerances": {"gouge": 0.002, "leftover": 0.005},
  "sim":        {"resolution": 0.01},
  "lint":       {"rapid_below_clearance": "error"},
  "drill_model": "cone"
}
```

- **stock**: a box in part (model) coordinates, given as `min`/`max` or `origin` + `size`. It can also be a
  path to a json file, and the `stockXLow..stockZHigh` keys from `camkit.stock_box()` (inches) are accepted.
  They are read as part coordinates. Check that on the first real Fusion job.
- **tools**: the native format below, the list from `camkit.tools_used()`, or raw Fusion tool JSON (a list
  or a library export with `data`). Fusion types are mapped (flat/ball/bull nose end mill, face mill, drill,
  spot/center drill, counter sink, chamfer mill (TA = half angle), tap, thread mill, reamer, counter bore,
  boring bar, slot/lollipop/dovetail). Holder `segments` become up to 8 stacked cylinders, and SFDM becomes a
  shank cylinder when it is larger than DC. A Fusion tool without a holder gets no holder check, and the
  report warns about it. `camkit.tools_used()` does not export SFDM or the tip diameter yet.
- **Each setup has its own transform** from part coordinates to its WCS:
  `wcs = R @ part + translation`. R is `rotation_deg` [rx, ry, rz] (extrinsic, X then Y then Z), or give a
  full `"matrix"` (4x4, row-major). The WCS +Z is the spindle axis. Example: OP2 above flips the part
  about X (part y becomes 4 - y, part z becomes -z), so the WCS origin sits at the part's bottom-left
  corner, now on top.
  - `wcs`: the work offset the program must select (checked against G54-G59/G154 in the code).
  - `min_tool_z`: WCS Z the tool tip must not go below (parallels, vise), reported as `below_min_z`.
  - `expect_finish`: whether leftover in this setup's own view fails. Default: true for single-setup jobs.
    With several setups the default is false, and the final leftover check decides instead.
  - `leftover_floor_z`: single-setup view only. Material outside the part below this WCS Z is not counted
    as leftover. Default: the part's lowest Z.
- **units / part_units**: OCCT reads STEP in millimetres, so STEP is scaled to `units` automatically. For
  STL/OBJ set `"part_units"`. The NC files may use G20 or G21. They are converted to `units`.
- **machine / tolerances / sim** override the defaults in `checks/job.py`. Lengths are in job units, and the
  inch defaults are converted when `units` is mm. Other tolerance keys: `leftover_area_pct_max` (0.5),
  `leftover_max` (0.02 in), `leftover_min_region_cells` (9), `rapid_contact` (0.0005 in),
  `holder_clearance` (0), and `rapid_clearance` (0.04 in above the stock top). Other sim keys:
  `margin`, `edge_band_cells` (1), `arc_tolerance`, `mesh_tolerance`, and `search_tolerance`.
- **lint**: set a rule's severity: `rapid_below_clearance` (`"error"`, or `"warning"` when the post keeps the
  tool down with G0 in cleared areas and you rely on stock_sim), `coolant` (`"warning"`/`"off"`),
  and `below_min_z`.
- **drill_model**: `"cone"` (point angle) or `"flat"`.

`tools.json` (lengths in its own `units`, converted to the job units):

```json
{"units": "inch", "tools": [
  {"number": 1, "type": "flat", "diameter": 0.5, "corner_radius": 0, "flute_length": 1.0,
   "stickout": 1.5, "holder_diameter": 1.5, "holder_length": 1.8},
  {"number": 2, "type": "drill", "diameter": 0.25, "point_angle": 118, "flute_length": 1.5,
   "stickout": 2.2, "holder_diameter": 1.0, "holder_length": 2.0}
]}
```

`type` is one of:
- flat, ball, bull (uses `corner_radius`)
- drill/spot/center (a cone from `point_angle`)
- chamfer/countersink (a cone from `point_angle` and `tip_diameter`)
- reamer/boring/face/counterbore (a flat cylinder)
- tap/thread_mill: these remove material with only 0.75 x their radius, which is below any tap drill. Any
  material they remove is reported as `thread_tool_in_solid` (a hole that was not drilled). Thread form is
  not simulated.
- slot/lollipop/dovetail: undercut tools. They are not simulated, and the report warns about them.

`stickout` is tip to holder face. The holder is a cylinder of `holder_diameter` x `holder_length` starting
there. Or give `holder_segments` as `[{"height", "diameter"}]` or Fusion `lower-/upper-diameter`, bottom-up,
plus optional `shank_diameter` and `shoulder_length`.

## What is reported

A job fails on any nc_lint error or any of these stock_sim results: a gouge cell, a rapid removing more than
`rapid_contact`, a shank or holder intrusion, a cut deeper than the flute length, a tap or thread mill in
solid material, a tool missing from the list, or leftover beyond the limits. Leftover fails when more than `leftover_area_pct_max` % of the part's projected
area is covered, or when a region of at least `leftover_min_region_cells` cells is taller than
`leftover_max`.

nc_lint rule ids are: `safe_start`, `units`, `wcs`, `toolchange`, `tool_unknown`, `g43_h`, `spindle`, `rpm`,
`feed`, `coolant`, `rapid_below_clearance`, `below_min_z`, `canned_cycle`, `arc`, `cutter_comp`,
`program_end`, `unsupported`, `unknown_code`, `no_decimal`, and `g28`. Each file also gets an estimated
cycle time: feed length / F plus rapid length / `max_rapid`, with no acceleration.

stock_sim reports per setup:
- `gouge`: removal below the part surface. It gives max depth (vertical), % of part area, regions, and the
  NC lines that made the deepest cut in the gouged cells.
- `leftover`: material above the part surface in this setup's view.
- `rapid_through_stock`, `holder_collision` (with `part`: shank or holder), `beyond_flute_length`,
  `thread_tool_in_solid`: the worst NC lines for each.
- `tools_not_simulated`, `tools_without_holder`.
- `per_tool`: removed volume, feed length, and air-cut %.

It also saves `<setup>_maps.npz` (sim, part, gouge, leftover, extent) and, with `--png`, an image.
`final` in the report is the part-frame leftover after all setups. It splits into from-top, from-bottom and
outside-part leftover.

## How it works (short)

- `gcode.py` interprets modal state per block. It handles G0/1/2/3 (IJK and R, G17/18/19, helical),
  G90/91, G20/21, work offsets, G43/49, G28/G53 (tip goes to `home_z_above_stock`, and XY becomes unknown
  after XY homing or M06), and M3/4/5/6/8/9/30. It expands canned cycles G73/G76/G81-G89 with G98/G99, Q
  pecks and L repeats. It flags macros (`#`), M97/M98 subprograms, G10 and G93/G95 as unsupported.
- `zmap.py` holds the numba kernels. For every tool-tip segment and every grid cell within the tool radius,
  it finds the lowest point of the tool's cutting surface over the segment. That is exact for level moves
  and flat tools. For ball, bull and cone moves with a Z change it uses a golden-section search (the
  function is convex). A lower-bound test skips cells the move cannot reach. The shank and holder are
  checked as stacked cylinders above the tip against the remaining height field. The part is rasterized top-down
  with a z-buffer at the same cell centres.
- Gouge and leftover compare against the part map with a one-cell edge band (min/max filter). That avoids
  false hits at vertical walls from tessellation and cell centring. Wall gouges narrower than one cell are
  not reported.
- In-process stock is a [bottom, top] interval per part-frame column. Setups along part +Z lower the top.
  Flipped setups (-Z) raise the bottom. The next setup starts from it, so holes drilled in OP1 are open
  in OP2.

## Performance (M-series Mac, 29.9.2026)

- Synthetic 6 x 4 in part (face, pocket, 4 pecked holes, 165 moves) at 0.01 in: about 1 s end to end.
  About 1.5 s with a cold numba cache. The numba cache is written to `checks/__pycache__`.
- A dense 3D program with 200k moves (bull + ball, whole 6 x 4 part) takes 1.5 s of sweep at 0.01 in and
  4 s at 0.005 in, or 3.3 s end to end including parsing.
- The full pytest suite runs 25 tests in about 7 s.

## Tried on real output

The Huron B208 Rev E programs from September (Fusion Haas NGC post, mm, 22k lines, archived) parse and lint
clean except for two things:
- The safe-start warning. The post emits `G90 G94 G17` without G40/G49/G80.
- `rapid_below_clearance` on 27 lines of S10. Those are Fusion's stay-down G0 moves inside the cleared
  adaptive area.

The S10 sweep takes 0.13 s at 0.25 mm. The stock and WCS were guessed, so this is not a gouge/leftover
validation against Fusion's result. That needs the job exported with its real per-setup transforms. The
post writes positioning as `G1 F5000` at home Z before G43, and lint treats that as positioning, not
cutting.

## Dependencies and Windows

Everything is pip-installable. There are no system packages.

- Required: numpy, scipy, and numba (llvmlite is native and has wheels for macOS arm64/x86 and Windows
  amd64). Without numba the kernels still run as pure Python, but about 100x slower.
- STEP: `cadquery-ocp` (native OCCT wheels, including win_amd64). STL/OBJ: `trimesh`.
- `matplotlib` is only needed for `--png`, and `pytest` for the tests. `cadquery` is used only by the
  self-test to build the synthetic STEP.

Windows AWS VM (untested there yet):
```powershell
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -r checks\requirements.txt
.venv\Scripts\python.exe -m pytest checks\tests -q
```

CAMotics (optional cross-check, not used): there is no Homebrew cask. `brew info --cask camotics` fails on
29.9.2026, and `brew search` finds only `camo-studio`. CAMotics ships its own macOS and Windows installers
from camotics.org. It could serve as an independent second simulator later, but nothing here depends on it.

## Limitations

- **3-axis Z-map only.** It cannot represent undercuts, side holes, T-slot/keyseat cutters or anything under
  an overhang. Such features look filled from above, so a gouge or leftover there is invisible.
- **Tilted (3+2) setups** are simulated from the raw stock box (as an AABB in that WCS). They do not update
  or read the in-process stock, and they disable the final part-frame check. Setups that rotate about Z
  or flip about X/Y are handled fully. Use `matrix` or `rotation_deg` per setup for all of them.
- Gouge depth is vertical. A wall gouge of 0.02 in into a 0.5 in wall reports 0.5 at those cells.
  Use the region location and the NC lines.
- The simulation follows the programmed path. G41/G42 cutter compensation is not applied (it is flagged).
  Tool geometry comes from tools.json, not from the H/D offset registers.
- The holder is at most 8 stacked cylinders (conservative merge). The spindle nose, vise, clamps and
  fixtures are not modelled. Use
  `min_tool_z` for the vise and parallels.
- Machine travel limits are not checked, because that needs the WCS position in machine coordinates.
  G28/G53 moves are assumed to be safe Z retracts.
- Chip load, engagement, and surface speed are not checked. The rpm and feed checks are machine limits
  only. Per-tool air-cut % and removed volume are reported as hints.
- Haas macros (#, G65/G66), subprograms (M97/M98), G10, G93/G95, G68 rotation, G51 scaling and the Haas
  pocket/engraving/bolt-pattern cycles (G12/G13/G47/G70-G72/G150) are not interpreted.
  Programs using them are flagged `unsupported`.
- Cells are sampled at their centres. Features smaller than about 2 cells are unreliable, so use `--res`
  for small parts.

### Side / tilted setups
A setup whose tool axis is not the part's +Z/-Z can't carry the in-process stock. If it runs after the other
faces are finished, add `"initial_stock": "part"` to that setup in job.json: it then starts from the finished
part seen from its own axis (no false leftover/rapid-through-stock from the raw stock box). Holes cut in that
setup must exist in the model for this to be exact.

## 3D voxel simulation (voxel_sim.py) — part of `checks run`
`python -m checks run JOB` now also runs the 3D voxel simulation across all setups (top, flip, side/end at
90°, tilted 3+2), carrying the in-process stock, and compares the final stock with the part in 3D with an
edge-break allowance (shallow chamfers at sharp edges are warnings). When it passes, Z-map gouge/leftover
failures become warnings (the voxel result is authoritative for the final part); the Z-map still decides
rapids through stock, holder collisions, flute length and taps in solid per setup. Standalone:
`python -m checks.voxel_sim JOB [--res 0.01] [--png]`. Thread mills and undercut tools (slot, lollipop,
dovetail) are not yet modelled correctly.
