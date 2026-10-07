# CNC check: FAIL

Job `/home/node/cnc/job` (inch), 10.08 s

## OP1 (G54): FAIL
Files: 1001.nc
- `1001.nc`: 259 moves, tools [1, 2], est. cycle 2.77 min
- stock_sim at 0.01 (raw stock box): gouge max 0.0 (0.0%), leftover max 0.5 (0.026%), rapids through stock 0, holder hits 0, beyond flute 0, thread tool in solid 0; 2.878 s

Failures:
- stock_sim leftover: max 0.5000 inch, 0.026% of part area, worst at XY [2.035, 1.535]

Warnings:
- nc_lint no_decimal: axis word without decimal point (Fanuc reads X1 as 0.0001 in; Haas as 1.0) (1001.nc:16, x10)
