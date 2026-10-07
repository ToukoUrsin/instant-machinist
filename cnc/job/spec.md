# Customer RFQ: PLATE-104 mounting plate

- Material: 6061-T6 aluminium. Quantity: 25.
- Finished part: 6.000 x 4.000 x 1.000 in block. Part zero = lower-left corner of the top face (X right, Y back, Z up). Part top is Z0, bottom is Z-1.000.
- Stock: 6.000 x 4.000 x 1.050 in saw-cut plate (0.050 in extra on top, already square on the sides). Held in a 6 in vise on parallels; the tool tip must never go below Z-1.150.
- Features (also in `part.step`):
  - Top face: face 0.050 in down to Z0.
  - Pocket: 3.000 x 2.000 in, centred at X3.000 Y2.000, 0.500 in deep (floor at Z-0.500), corner radius R0.3125, flat floor, vertical walls.
  - Four Ø0.250 in through holes at X0.5 Y0.5, X5.5 Y0.5, X5.5 Y3.5, X0.5 Y3.5.
- Machine: Haas VF-2 (8,100 rpm, 650 ipm feed). One setup, work offset G54. Inch (G20).
- Tools available (`tools.json`): T1 0.500 in 3FL flat end mill, T2 0.250 in jobber drill, T3 5/16-18 tap (not needed).
