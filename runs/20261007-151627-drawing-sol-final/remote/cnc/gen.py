"""PLATE-104 drawing geometry; explicit Haas tool-center paths in inches."""
from pathlib import Path
import math

ROOT = Path(__file__).resolve().parent
lines = ['%', 'O1001 (PLATE-104 REV A)', 'G00 G17 G40 G49 G80 G90',
         'G20 G54 G94']

def emit(s):
    lines.append(s)

def move(g, x=None, y=None, z=None, f=None, i=None, j=None):
    s = g
    for k, v in [('X', x), ('Y', y), ('Z', z), ('I', i), ('J', j), ('F', f)]:
        if v is not None:
            s += f' {k}{v:.4f}'
    emit(s)

def tool(n, rpm):
    emit(f'T{n} M06')
    emit(f'S{rpm} M03')
    emit(f'G00 G43 H{n} Z1.')
    emit('M08')

def finish_tool():
    emit('G00 Z1.')
    emit('M09')
    emit('M05')
    emit('G49')

def face(step, feed):
    # Full cutter runs outside edges before descending or traversing Y.
    count = math.ceil(4 / step)
    for k in range(count + 1):
        y = 4 * k / count
        x0, x1 = (-.5, 6.5) if k % 2 == 0 else (6.5, -.5)
        move('G00', z=.15)
        move('G00', x=x0, y=y)
        move('G01', z=0, f=12)
        move('G01', x=x1, f=feed)
    move('G00', z=.15)

def perimeter(z, feed):
    # Clockwise internal climb contour, tool-center corner radius .0625.
    r = .3125 - .5 / 2
    move('G01', x=1.8125, y=1.25, z=z, f=feed)
    move('G02', x=1.75, y=1.3125, i=0, j=r, f=feed)
    move('G01', y=2.6875)
    move('G02', x=1.8125, y=2.75, i=r, j=0)
    move('G01', x=4.1875)
    move('G02', x=4.25, y=2.6875, i=0, j=-r)
    move('G01', y=1.3125)
    move('G02', x=4.1875, y=1.25, i=-r, j=0)
    move('G01', x=1.8125)

emit('(FACE ROUGH AND SPRING FINISH)')
tool(1, 6000)
face(.2, 35)
face(.15, 25)
emit('(POCKET RAMP RASTER AND WALL FINISH)')
prev = 0.0
for depth in [-.1, -.2, -.3, -.4, -.49, -.5]:
    move('G00', z=.15)
    move('G00', x=2.5, y=2.0)
    move('G01', z=prev, f=12)
    # Repeated zig-zag ramps: <= .025 axial drop over 1 inch.
    n = math.ceil(abs(depth - prev) / .025 - 1e-9)
    for k in range(1, n + 1):
        x = 3.5 if k % 2 else 2.5
        move('G01', x=x, z=prev + (depth - prev) * k / n, f=12)
    cut_feed = 20 if depth == -.5 else 30
    move('G01', x=1.8125, y=1.25, f=cut_feed)
    count = math.ceil(1.5 / .18)
    for k in range(count + 1):
        y = 1.25 + 1.5 * k / count
        x0, x1 = (1.8125, 4.1875) if k % 2 == 0 else (4.1875, 1.8125)
        move('G01', x=x0, y=y, f=cut_feed)
        move('G01', x=x1)
    perimeter(depth, 18)
    prev = depth
perimeter(-.5, 18)
finish_tool()
emit('(FOUR QUARTER INCH PLAIN THROUGH HOLES NO TAP)')
tool(2, 2500)
move('G00', x=.5, y=.5)
move('G00', z=.15)
emit('G98 G83 X0.5000 Y0.5000 Z-1.1000 R0.1500 Q0.1250 F10.0000')
for x, y in [(5.5, .5), (5.5, 3.5), (.5, 3.5)]:
    emit(f'X{x:.4f} Y{y:.4f}')
emit('G80')
finish_tool()
emit('M30')
emit('%')
(ROOT / 'job/1001.nc').write_text('\n'.join(lines) + '\n')
print(f'Generated job/1001.nc: {len(lines)} lines')
