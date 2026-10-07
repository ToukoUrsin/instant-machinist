# Operations
1. T1: .500 flat 3-flute end mill, 1.000 flute, 1.500 stickout, 1.500 holder. Face raw Z+.050 to finished Z0 using overlapping .200 stepover passes at 6000 rpm/35 ipm, then .150 stepover spring finish at 25 ipm. Approach/retract outside stock at X-.500/X6.500.
2. T1: pocket with linear ramps at 12 ipm, maximum .025 descent per 1.000 horizontal travel. Clear in .100 axial layers, .180 raster stepover, 30 ipm, followed by exact rounded perimeter at 18 ipm; final floor layer Z-.500 from -.490 at 20 ipm. Tool-center perimeter X1.750..4.250, Y1.250..2.750, corner radius .0625 computed from drawing R.3125 minus tool radius .250. Tool fits with 80% radius ratio. Separate finish contour at Z-.500. Coolant on.
3. T2: .250 118-degree jobber drill, 1.500 flute, 2.200 stickout, 1.000 holder. Four plain through holes with G98 G83 at 2500 rpm, 10 ipm, Q.125, R.150, tip Z-1.100. Drill point clears bottom at full diameter; stays above -1.150 vise/parallels limit. No tapping: T3 is not a drawing requirement.

Z1.000 tool-change clearance, Z.150 local travel clearance; retract Z before XY travel. G20/G54/G90/G17, tool length offsets H1/H2, G94 feeds per minute, no cutter compensation (explicit tool-center geometry).

Crib has no chamfer/deburr tool and the one setup cannot access bottom edges. Edge break .005-.015 and all-burr removal are not completed by this nominal-geometry NC. Surface finish and no visible top tool marks must be physically checked. Simulation does not certify these notes; package records that limitation rather than asserting compliance.
