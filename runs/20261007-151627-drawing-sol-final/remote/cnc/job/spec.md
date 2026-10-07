# PLATE-104 Rev A — drawing-governed specification

Drawing: Bayline Automation mounting plate, J. Ortega 10/02/26, inch, third-angle, sheet 1/1. Material AL 6061-T6 plate; quantity 25; finish as machined. Interpret per ASME Y14.5-2018.

G54: lower-left finished top corner, X right, Y back, Z up. Finished top Z0; bottom Z-1.000.

| Feature | Nominal geometry and G54 location | Tolerance |
|---|---|---|
| Plate | X0..6.000, Y0..4.000, Z-1.000..0; 6.000 x 4.000 x 1.000 | Each dimension +/- .005 |
| Pocket | 3.000 x 2.000, X1.500..4.500, Y1.000..3.000; center (3,2); floor Z-.500 | Size, 1.500/1.000 location dimensions and .500 depth +/- .005 |
| Pocket corners | 4X R.3125; arc centers (1.8125,1.3125), (4.1875,1.3125), (4.1875,2.6875), (1.8125,2.6875) | Four-decimal tolerance not stated |
| Lower-left hole | Diameter .250 THRU, center (.500,.500), Z0..-1.000 | Diameter and shown .500 offsets +/- .005 |
| Lower-right hole | Diameter .250 THRU, center (5.500,.500), Z0..-1.000 | Diameter and shown .500 offsets +/- .005 |
| Upper-left hole | Diameter .250 THRU, center (.500,3.500), Z0..-1.000 | Diameter +/- .005; upper-row location not explicitly dimensioned in drawing |
| Upper-right hole | Diameter .250 THRU, center (5.500,3.500), Z0..-1.000 | Diameter +/- .005; upper-row location not explicitly dimensioned in drawing |

General tolerances: .XXX +/- .005, .XX +/- .010, angles +/- 0.5 degree. All surfaces 125 Ra maximum; pocket floor 63 Ra (roughness units not printed; conventionally microinch). Pocket walls vertical, floor flat; no separate numeric geometric tolerance. Break ALL sharp edges .005-.015; remove ALL burrs; NO TOOL MARKS ON TOP FACE. STEP reference only; drawing governs.

## Cross-check and unresolved requirements
- Read STEP with OpenCascade, converting native millimetres to inches. Envelope 0..6 x 0..4 x 0..1. Transform Z by -1 as job.json prescribes: matches shop zero and drawing thickness.
- STEP pocket walls X1.5/4.5 and Y1/3, floor model Z.5 (G54 Z-.5), corner cylindrical radii .3125 and centers listed above: match drawing.
- STEP four hole radii .125 at the listed XY coordinates: matches hole size and symmetric pattern. Upper-row coordinates are taken from reference STEP consistent with the drawing illustration, not independently dimensioned customer requirements.
- Shop stock 6 x 4 x 1.050, extra .050 on top only: G54 raw top +.050, bottom -1.000. Sides already square/to size; facing required, no exterior profile milling needed.
- Haas VF-2 limits 8100 rpm, 650 ipm; one vise/parallels setup; absolute tool-tip floor Z-1.150. job.json agrees.
- No dimensional/model conflicts found. Drawing omissions: upper-hole location dimensions, four-decimal radius tolerance, roughness units, numeric flatness/perpendicularity. Edge breaks and burr removal (especially bottom edges) cannot be claimed from nominal STEP stock-removal alone. No-tool-marks and Ra require physical inspection.

Operation/tool plan follows crib review in process_plan.md.
