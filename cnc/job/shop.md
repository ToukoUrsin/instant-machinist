# Shop job sheet: PLATE-104 (filled in by the shop, not the customer)

- Customer drawing: `drawing.png` (also `drawing.pdf`). 3D model: `part.step`. The drawing governs.
- Stock: 6.000 x 4.000 x 1.050 in saw-cut 6061-T6 plate (0.050 in extra on top only; sides already square and to size).
- Workholding: 6 in vise on parallels. The tool tip must never go below Z-1.150.
- Part zero (G54): lower-left corner of the finished top face. X right, Y back, Z up. Finished top is Z0, bottom Z-1.000.
- Machine: Haas VF-2, 8,100 rpm max, 650 ipm max feed. One setup. Inch (G20).
- Tool crib: `tools.json` (T1 0.500 in 3FL flat end mill, T2 0.250 in jobber drill, T3 5/16-18 tap).
