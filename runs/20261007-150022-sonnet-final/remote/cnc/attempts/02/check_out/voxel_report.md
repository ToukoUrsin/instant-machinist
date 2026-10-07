# Voxel check: FAIL

Job `/home/node/cnc/job` (inch), 0.01 inch voxels, grid [604, 404, 109] (26.6 M), 5.65 s

## OP1 (G54): exact (tool axis = part +Z)
- 1001.nc: 259 moves, 412 segments, removed 3.2264 inch^3, sweep 0.025 s
- rapids into stock 0, holder/shank hits 0, beyond flute 0, thread tool in solid 0

## Final part vs. stock (part frame, all setups)
- gouge: 0 voxels, max depth 0.0 inch, 0.0% of part volume
- edge break (warning): 0 voxels, max depth 0.0 inch
- leftover: 1101200 voxels, max 0.34 inch from the part surface, local thickness up to 0.34384 inch, 5.2819% of part volume
  - 1101200 vox (1.1012 inch^3), max 0.34 from part (local 0.34384) at [1.845, 2.665, 0.995], bbox [1.5, 1.0, 0.5]..[4.49, 2.99, 1.0]

## Failures
- leftover: max 0.3400 inch thick (+-0.005), 5.2819% of part volume, 1 region(s) over 0.02; worst at part XYZ [1.845, 2.665, 0.995]
