# PLATE-104 — verified simulation package

- Program: `job/1001.nc` (O1001); generator: `gen.py`. Haas inch/G54, absolute XY/Z, explicit tool-center paths, flood coolant. T3 unused.
- **Face — T1 Ø0.500 flat, 3 flutes:** 6,000 rpm, 40 ipm; 13 raster passes, nominal 0.3333 in stepover, remove 0.050 in to Z0. Feed approach 10 ipm.
- **Pocket — T1:** ramp entry 15 ipm; raster roughing 30 ipm to Z-0.120/-0.240/-0.360/-0.480, leaving 0.020 in wall/floor stock. Finish floor at Z-0.500 with 30 ipm raster; walls at 25 ipm, exact G03 corner offsets (tool-center R0.0625), followed by a wall spring pass. Z approach 10 ipm.
- **Four through holes — T2 Ø0.250, 118° drill:** 2,500 rpm, 8 ipm, G98 G83, R0.100, Q0.200, tip Z-1.100. Full-diameter breakthrough accounts for the drill point; tip remains above the Z-1.150 setup limit. Retract to Z1 between holes.

## Attempts and result
1. **FAIL (verifier dependency, not an NC finding):** STEP loading crashed because installed cadquery-ocp 8.0.1.1.0 lacked `TopoDS.Face_s`. No material-removal report was produced. Installed compatible cadquery-ocp 7.8.1.1.post1 in the existing `.venv` (vtk 9.3.1 dependency); independently confirmed real STEP tessellation yielded 2,572 triangles. No protected source, model, or job/tool data was changed. NC geometry was unchanged.
2. **PASS:** `attempts/02/check_out/report.md`; 244 simulated moves at 0.010 in resolution. Reported gouge and leftover maxima both 0.0; zero rapids through stock, holder hits, beyond-flute events, or thread-tool-in-solid events. Estimated cycle **8.58 min per part** (not setup/load time). Generator contract test also passed.

## Machinist double-checks
- Confirm G54 zero at the finished top's lower-left corner, H1/H2 offsets, actual tool diameters/stickout, center-cutting/ramp capability of T1, coolant and chip evacuation.
- Confirm real vise/jaw/parallels clearance, particularly underside hole breakthrough and off-edge facing. Stock-model simulation is not proof of unmodeled fixture or machine-travel clearance. Establish a safe starting machine position before initial XY positioning.
- Report has one non-failing warning at NC line 196: no G28/G53 home-Z retract before M30. Program ends at G54 Z1.000; it does not home the machine. Retained exactly as verified.
- Simulation resolution is 0.010 in; inspect actual pocket size/radius, floor depth, finish, hole diameter and breakthrough against the customer's requirements. No finer dimensional tolerance was supplied.
