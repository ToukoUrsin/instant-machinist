"""PLATE-104: explicit inch tool-center paths for Haas VF-2."""
from pathlib import Path
import math

ROOT = Path(__file__).resolve().parent
lines = ['%', 'O1001 (PLATE-104 6061-T6)',
         'G00 G17 G40 G49 G80 G90', 'G20 G54 G94 G98']

def emit(s):
    lines.append(s)

def move(code, x=None, y=None, z=None, f=None, i=None, j=None):
    words = [code]
    for letter, value in [('X', x), ('Y', y), ('Z', z), ('I', i), ('J', j)]:
        if value is not None:
            words.append(f'{letter}{value:.4f}')
    if f is not None:
        words.append(f'F{f:.1f}')
    emit(' '.join(words))

def contour(z, allowance, feed):
    # Exact inward offset of the R0.3125 pocket by tool radius + stock.
    r = .3125 - .25 - allowance
    left, right = 1.75 + allowance, 4.25 - allowance
    bottom, top = 1.25 + allowance, 2.75 - allowance
    move('G01', left+r, bottom, f=feed)
    move('G01', right-r, bottom)
    move('G03', right, bottom+r, i=0, j=r)
    move('G01', right, top-r)
    move('G03', right-r, top, i=-r, j=0)
    move('G01', left+r, top)
    move('G03', left, top-r, i=0, j=-r)
    move('G01', left, bottom+r)
    move('G03', left+r, bottom, i=r, j=0)

emit('(T1 FACE 0.050 TO Z0 - 0.3333 STEPOVER)')
emit('T1 M06')
emit('S6000 M03')
move('G00', -.30, 0)
emit('G43 H1 Z1.')
emit('M08')
move('G00', z=.15)
move('G01', z=0, f=10)
for row in range(13):
    y = row * 4 / 12
    end_x = 6.30 if row % 2 == 0 else -.30
    move('G01', y=y, f=40)
    move('G01', x=end_x)
move('G00', z=1)

emit('(POCKET ROUGH 0.020 WALL/FLOOR STOCK THEN FINISH)')
previous = 0
for z in [-.12, -.24, -.36, -.48, -.50]:
    finish = z == -.50
    emit(f'(POCKET DEPTH {z:.4f})')
    move('G00', x=1.85, y=1.35)
    move('G00', z=.15)
    move('G01', z=previous, f=10)
    # Ramp into each layer within the cleared central pocket footprint.
    move('G01', x=4.15, z=z, f=15)
    move('G01', x=1.85, f=30)
    for row in range(1, 6):
        move('G01', y=1.35 + row * .26, f=30)
        move('G01', x=4.15 if row % 2 else 1.85)
    contour(z, 0 if finish else .02, 25 if finish else 30)
    if finish:
        contour(z, 0, 25)  # Wall spring pass.
    move('G00', z=1)
    previous = z

emit('M09')
emit('M05')
emit('(T2 FOUR THROUGH HOLES - 118 DEG POINT BREAKTHROUGH)')
emit('T2 M06')
emit('S2500 M03')
move('G00', x=.5, y=.5)
emit('G43 H2 Z1.')
emit('M08')
# Drill tip extension for full-diameter breakthrough: R / tan(59 deg).
point = .125 / math.tan(math.radians(59))
assert 1.10 > 1 + point and -1.10 >= -1.15
emit('G98 G83 X0.5000 Y0.5000 Z-1.1000 R0.1000 Q0.2000 F8.0')
emit('X5.5000 Y0.5000')
emit('X5.5000 Y3.5000')
emit('X0.5000 Y3.5000')
emit('G80')
move('G00', z=1)
emit('M09')
emit('M05')
emit('M30')
emit('%')
(ROOT / 'job/1001.nc').write_text('\n'.join(lines) + '\n')
print(f'Wrote job/1001.nc: {len(lines)} blocks')
