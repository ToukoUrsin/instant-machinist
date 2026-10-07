# Voxel check: FAIL

Job `/home/node/cnc/job` (inch), 0.01 inch voxels, grid [604, 404, 109] (26.6 M), 6.21 s

## OP1 (G54): exact (tool axis = part +Z)
- 1001.nc: 259 moves, 404 segments, removed 4.34825 inch^3, sweep 1.39 s
- rapids into stock 0, holder/shank hits 0, beyond flute 0, thread tool in solid 0

## Final part vs. stock (part frame, all setups)
- gouge: 0 voxels, max depth 0.0 inch, 0.0% of part volume
- edge break (warning): 0 voxels, max depth 0.0 inch
- leftover: 3150 voxels, max 0.5 inch from the part surface, local thickness up to 0.03 inch, 0.0151% of part volume
  - 1050 vox (0.00105 inch^3), max 0.5 from part (local 0.03) at [3.975, 2.475, 0.995], bbox [3.92, 2.42, 0.5]..[3.98, 2.48, 1.0]
  - 1050 vox (0.00105 inch^3), max 0.5 from part (local 0.03) at [2.055, 1.545, 0.995], bbox [2.02, 1.52, 0.5]..[2.08, 1.58, 1.0]
  - 1050 vox (0.00105 inch^3), max 0.5 from part (local 0.03) at [2.055, 2.475, 0.995], bbox [2.02, 2.42, 0.5]..[2.08, 2.48, 1.0]

## Failures
- leftover: max 0.5000 inch thick (+-0.005), 0.0151% of part volume, 3 region(s) over 0.02; worst at part XYZ [3.975, 2.475, 0.995]
