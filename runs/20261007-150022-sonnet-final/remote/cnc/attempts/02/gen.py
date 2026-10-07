CX,CY=3.0,2.0
o=[]
def w(s): o.append(s)
def f(v):
    t=f"{v:.4f}".rstrip('0')
    return t+"0" if t.endswith('.') else t
def loop(hx,hy,r,z,F):
    X=lambda x:CX+x; Y=lambda y:CY+y
    pts=[(hx,-(hy-r)),(hx,hy-r)]
    w(f"G01 X{f(X(hx))} Y{f(Y(-(hy-r)))} F{F*2}")
    w(f"G01 Y{f(Y(hy-r))} F{F}")
    w(f"G03 X{f(X(hx-r))} Y{f(Y(hy))} I{f(-r)} J0")
    w(f"G01 X{f(X(-(hx-r)))}")
    w(f"G03 X{f(X(-hx))} Y{f(Y(hy-r))} I0 J{f(-r)}")
    w(f"G01 Y{f(Y(-(hy-r)))}")
    w(f"G03 X{f(X(-(hx-r)))} Y{f(Y(-hy))} I{f(r)} J0")
    w(f"G01 X{f(X(hx-r))}")
    w(f"G03 X{f(X(hx))} Y{f(Y(-(hy-r)))} I0 J{f(r)}")
w("%"); w("O1001 (PLATE-104)")
w("G00 G17 G40 G49 G80 G90"); w("G20 G54")
# T1 face + pocket
w("T1 M06"); w("S8000 M03"); w("G00 G54 X-0.45 Y0.2"); w("G43 H1 Z1."); w("M08")
w("G00 Z0.1"); w("G01 Z0. F40.")
y=0.2; d=1
while y<=3.8001:
    w(f"G01 Y{f(y)} F60.")
    w(f"G01 X{f(6.45 if d>0 else -0.45)} F60.")
    y+=0.4; d=-d
    if y>3.8001: break
w("G00 Z0.1")
HX,HY,R=1.0,0.5,0.0625
w(f"G00 X{f(CX-0.5)} Y{f(CY)}"); w("G00 Z0.1"); w("G01 Z0.02 F40.")
zprev=0.0
for z in (-0.25,-0.5):
    w(f"G01 X{f(CX-0.5)} Y{f(CY)} Z{f(zprev)} F40.")
    w(f"G01 X{f(CX+0.5)} Z{f(z)} F25.")
    w(f"G01 X{f(CX-0.5)} F40.")
    w(f"G01 X{f(CX+0.5)} F60.")
    for dd in (0.4,0.2,0.02):
        hx,hy=HX-dd,HY-dd
        loop(hx,hy,min(R+dd,hy-0.001),z,50.)
    w(f"G01 X{f(CX)} Y{f(CY)} F100.")
    zprev=z
# finish wall at -0.5
w(f"G01 X{f(CX-0.5)} Y{f(CY)} F100.")
w(f"G01 X{f(CX+HX-0.05)} Y{f(CY-HY+R)} F100.")
loop(HX,HY,R,-0.5,40.)
w(f"G01 X{f(CX+HX-0.1)} F100.")
w("G00 Z1."); w("M09"); w("G91 G28 Z0."); w("G90")
# T2 drill
w("T2 M06"); w("S4000 M03"); w("G00 G54 X0.5 Y0.5"); w("G43 H2 Z1."); w("M08")
w("G83 X0.5 Y0.5 Z-1.1 R0.1 Q0.1 F12.")
for p in ((5.5,0.5),(5.5,3.5),(0.5,3.5)): w(f"X{p[0]} Y{p[1]}")
w("G80"); w("G00 Z1."); w("M09"); w("G91 G28 Z0."); w("G90"); w("M05"); w("G28 G91 Y0."); w("G90"); w("M30"); w("%")
open("job/1001.nc","w").write("\n".join(o)+"\n")
