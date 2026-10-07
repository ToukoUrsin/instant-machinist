# PLATE-104 – summary (PASS on attempt 3)
**Program** job/1001.nc (gen.py), Haas, G20/G54.
- T1 0.5 flat, S8000: face 0.050 down to Z0 (zig-zag, 0.4 step, F60); pocket 3x2 R0.3125 in two 0.25 ramped passes (Z-0.25, Z-0.5; ramp F25, cut F50), 4 offset contour loops per level (tool-centre path R0.0625 final), finishing wall pass at full depth F40 (~0.02 in left for it).
- T2 0.25 drill, S4000: G83 Z-1.1 (through, tip ≥ Z-1.15 limit) R0.1 Q0.1 F12, 4 holes.
**Attempts**
1. Simulator could not run: venv's OCP 8.0 lacked `TopoDS.Face_s`; fixed environment by installing cadquery-ocp 7.8.1 (verifier untouched). Then FAIL: 0.5 in leftover at pocket centre (XY 2.035,1.535) – my pocket loops left an uncut island. Also no-decimal lint warnings (feed values like X1).
2. Fixed decimal formatting; tried fewer/larger loops – worse (9% leftover, island at 1.985,1.215).
3. Added loops at 0.2 in stepover from wall inward (offsets 0.6/0.4/0.2/0.02) → PASS, 0 gouge, 0 leftover, 0 holder/flute/rapid issues. Est. cycle 3.03 min.
**Check at machine:** pocket-centre zig-zag clearance covers the middle via ramp+offset loops; first-article check of pocket size and hole positions; chip evacuation in pocket (flood/air); tool-length offsets H1/H2 and G54 Z at top of stock-based zero (part top Z0, stock top Z0.05); drill peck/burr on bottom face; vise clamp clearance of parallels under drill exit (Z-1.1 goes 0.1 below part bottom – ensure parallels don't sit under holes). Feeds are conservative, untuned.
