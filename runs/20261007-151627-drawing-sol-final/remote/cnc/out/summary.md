# PLATE-104 Rev A — simulation-verified NC package

**Shop verifier PASS, attempt 01.** Reports: `attempts/01/check_out/report.md` and `report.json`. Nominal model geometry and programmed moves passed; this is not certification of every drawing note or permission for unattended machine execution.

## Drawing and cross-check
6061-T6, 25 pieces. Plate 6.000 x 4.000 x 1.000; centered 3.000 x 2.000 pocket, .500 deep, 4X R.3125; four diameter .250 through holes. G54 finished top lower-left: pocket X1.500..4.500/Y1.000..3.000/Z-.500; holes (.500,.500), (5.500,.500), (.500,3.500), (5.500,3.500). Three-decimal dimensions +/- .005. Shop raw top Z+.050, finished top Z0, bottom Z-1.000. OpenCascade STEP geometry agrees. Full feature/specification table is `job/spec.md`.

## Programmed operations
- T1 .500 flat 3FL: face .050 allowance to Z0, 6000 rpm, 35 ipm, .200 stepover; spring finish 25 ipm/.150 stepover.
- T1 pocket: linear ramps 12 ipm, at most .025 drop per inch; layers Z-.100/-.200/-.300/-.400/-.490/-.500. Raster .167 actual stepover (bounded by .180), 30 ipm rough/20 ipm final floor. Rounded walls and final spring contour 18 ipm, computed centerline radius .0625. Floor Z-.500.
- T2 .250 118-degree drill: four G98 G83 holes, 2500 rpm/10 ipm, Q.125, R.150, tip Z-1.100 (full-diameter breakthrough; limit Z-1.150). T3 tap unused: drawing specifies plain holes.
- Haas G20/G54/G90/G17/G94; required safe start, tool changes, G43 H1/H2, coolant, G80, M30. Tool diameter compensation is explicitly computed; no G41/G42.

## Attempts and measured result
01: PASS first run; no failed simulator attempts or corrective changes. Required NC acceptance test failed before generation (missing NC) and passed afterward.
- Estimated programmed cycle: **24.55 min per part**, excluding handling/inspection/deburring.
- Z-map resolution .010: zero gouge, zero leftover, zero stock-crossing rapids, zero holder/shank hits, zero flute-length violations.
- Independent 3D voxel result: PASS, zero gouge/leftover voxels, no warnings.
- One NC lint warning: no machine-home Z retract before M30, line 457. Program retracts to G54 Z1.000; machine-specific home/fixture clearance must be checked before release.

## Machinist/customer release checks — not certified by simulator
- Upper-row hole positions are taken from STEP/symmetry; drawing lacks explicit upper-row location dimensions. R.3125 has no four-decimal general tolerance. Clarify these with customer before production acceptance.
- **All-edge .005-.015 breaks and complete burr removal are NOT programmed.** Crib has no chamfer/deburr tool, and bottom edges cannot be reached in the prescribed single setup. Arrange an approved downstream deburr operation; this package alone is not a fully finished drawing-compliant part.
- Verify 125 Ra surfaces/63 Ra floor (units unstated), flat floor/vertical walls, and no visible top-face tool marks. Simulation cannot prove roughness, burrs, deflection or finish.
- Confirm real T1/T2 diameters, H offsets, G54, vise/jaw/parallels geometry and breakthrough clearance. Job simulator checks the stated Z limit and tool holders, not a complete physical fixture model.

Deliverables: `gen.py`, `job/1001.nc`, `job/spec.md`, `job/process_plan.md`, unit acceptance test, immutable attempt 01 snapshot, and original verifier reports. Protected job inputs and vendor code were not edited.
