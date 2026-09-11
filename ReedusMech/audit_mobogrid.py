"""REV4 audit, universal hex grid mobodeck + floating epoxy standoff pucks."""
import math, random

# --- mirrored from reedus_chassis.scad ---
deck_x   = 227.0
deck_y0, deck_y1 = -116.0, 88.0
mobo_y0, mobo_y1 = deck_y0 - 6, deck_y1          # -122 .. 88
mobo_deck_t = 4.0
hex_af, hex_wall = 12.0, 3.5
hex_r  = hex_af / math.sqrt(3)
hex_r2 = hex_r + hex_wall / math.sqrt(3)
hex_dx = 1.5 * hex_r2
hex_dy = math.sqrt(3) * hex_r2
hex_band = 11.0
puck_af = 16.0
puck_r  = puck_af / math.sqrt(3)
inner_w = 220.0
wall_x  = inner_w/2 - 2          # 108
bat_off_y = -18.0
leg_y   = [bat_off_y - 75, bat_off_y + 75]       # -93, 57
m3_clear = 3.4
mobo_l, mobo_w = 215.0, 190.0

def cells():
    gcy  = (mobo_y0 + mobo_y1) / 2
    xlim = deck_x/2 - hex_band
    out = []
    for i in range(-9, 10):
        cx = i * hex_dx
        for j in range(-9, 10):
            cy = gcy + j*hex_dy + (0 if i % 2 == 0 else hex_dy/2)
            if (cx - hex_r >= -xlim and cx + hex_r <= xlim and
                cy - hex_af/2 >= mobo_y0 + hex_band and
                cy + hex_af/2 <= mobo_y1 - hex_band):
                out.append((cx, cy))
    return out

def in_hex(px, py, cx, cy, r):
    """point-in-regular-hexagon, vertices at 0/60/... deg (OpenSCAD $fn=6)"""
    x, y = abs(px - cx), abs(py - cy)
    if x > r or y > r * math.sqrt(3)/2: return False
    return r*math.sqrt(3)/2 * r - (r*math.sqrt(3)/2)*x - (r/2)*y >= 0 or \
           (x <= r/2 and y <= r*math.sqrt(3)/2)

def in_hex2(px, py, cx, cy, r):
    x, y = abs(px-cx), abs(py-cy)
    return (y <= r*math.sqrt(3)/2) and (r*math.sqrt(3)/2*(r - x) >= r/2*y*math.sqrt(3)/2*0 + 0) and \
           (math.sqrt(3)*(r - x) >= y) if x > r/2 else (y <= r*math.sqrt(3)/2 and x <= r)

C = cells()
print(f"1. GRID")
print(f"   cells: {len(C)}   pitch {hex_dx:.2f} x {hex_dy:.2f} mm")
xs = [c[0] for c in C]; ys = [c[1] for c in C]
print(f"   extent x [{min(xs)-hex_r:.1f}, {max(xs)+hex_r:.1f}]  (plate +/-{deck_x/2})")
print(f"   extent y [{min(ys)-hex_af/2:.1f}, {max(ys)+hex_af/2:.1f}]  (plate {mobo_y0} .. {mobo_y1})")
# rib width between vertical neighbours in the same column
same_col = sorted(y for x,y in C if abs(x-0) < 0.01)
print(f"   vertical rib: {same_col[1]-same_col[0]-hex_af:.2f} mm (target {hex_wall})")
d = math.hypot(hex_dx, hex_dy/2)
print(f"   diagonal centre-centre {d:.2f} mm -> rib {d - 2*(hex_af/2):.2f} mm")

print(f"\n2. WALL SCREW CLEARANCE  (4x M3 at x=+/-{wall_x}, y={leg_y})")
worst = 1e9
for sx in (-1, 1):
    for y in leg_y:
        sxp, syp = sx*wall_x, y
        for cx, cy in C:
            gap = math.hypot(sxp-cx, syp-cy) - hex_r - m3_clear/2
            worst = min(worst, gap)
print(f"   min clearance screw edge -> nearest cell: {worst:.1f} mm  {'OK' if worst > 0 else '*** COLLIDES ***'}")

print(f"\n3. PUCK ANCHORING  (puck AF {puck_af}, cell circumdia {2*hex_r:.2f})")
random.seed(3)
NP = 4000
pts = []
while len(pts) < NP:
    x, y = random.uniform(-puck_r, puck_r), random.uniform(-puck_r, puck_r)
    ax, ay = abs(x), abs(y)
    if ay <= math.sqrt(3)*min(puck_r - ax, puck_r/2):
        pts.append((x, y))
gcy = (mobo_y0 + mobo_y1)/2
worst_frac, worst_pos = 1.0, None
N = 40
for a in range(N):
    for b in range(N):
        px = a/N * hex_dx * 2
        py = gcy + b/N * hex_dy
        near = [c for c in C if abs(c[0]-px) < 30 and abs(c[1]-py) < 30]
        if not near: continue
        solid = 0
        for dx, dy in pts:
            qx, qy = px+dx, py+dy
            hit = False
            for cx, cy in near:
                ax, ay = abs(qx-cx), abs(qy-cy)
                if ay <= math.sqrt(3)*min(hex_r-ax, hex_r/2):
                    hit = True; break
            if not hit: solid += 1
        f = solid/NP
        if f < worst_frac: worst_frac, worst_pos = f, (px, py)
puck_area = 1.5*math.sqrt(3)*puck_r**2
print(f"   worst-case rib contact under a puck: {worst_frac*100:.1f}% of puck footprint")
print(f"   = {worst_frac*puck_area:.0f} mm^2 of bonded area (puck footprint {puck_area:.0f} mm^2)")
print(f"   worst position {worst_pos[0]:.1f}, {worst_pos[1]:.1f}")
print(f"   {'OK - puck can never fall through or float free' if worst_frac > 0.15 else '*** TOO LITTLE CONTACT ***'}")

print(f"\n4. BOARD COVERAGE  (215 x 190 at y offset {bat_off_y})")
bx0, bx1 = -mobo_l/2, mobo_l/2
by0, by1 = bat_off_y - mobo_w/2, bat_off_y + mobo_w/2
print(f"   board  x[{bx0},{bx1}]  y[{by0},{by1}]")
print(f"   plate  x[{-deck_x/2},{deck_x/2}]  y[{mobo_y0},{mobo_y1}]")
# Minimum hole inset actually used by ATX-family boards: 0.25" from a side edge,
# 0.400" (10.16 mm) from the rear edge -- microATX Interface Spec v1.2, Fig 3/7.
SPEC_SIDE_INSET = 0.25 * 25.4   # 6.35
SPEC_REAR_INSET = 0.400 * 25.4  # 10.16
ok = True
for lbl, margin, inset in [("-X", bx0 - (-deck_x/2), SPEC_SIDE_INSET),
                           ("+X", deck_x/2 - bx1,    SPEC_SIDE_INSET),
                           ("-Y (I/O)", by0 - mobo_y0, SPEC_REAR_INSET),
                           ("+Y", mobo_y1 - by1,       SPEC_SIDE_INSET)]:
    # support available to a puck on the innermost credible hole
    have = margin + inset
    good = have >= puck_af/2
    ok &= good
    flush = margin >= puck_af/2
    print(f"   {lbl:9s} plate margin {margin:5.1f} + min spec inset {inset:5.2f}"
          f" = {have:5.1f} mm  (need {puck_af/2})  {'OK' if good else '*** SHORT ***'}"
          f"{'' if flush else '   [hole must be >= %.1f mm in from PCB edge]' % (puck_af/2 - margin)}")
print(f"   -> puck fully supported for every credible hole position: {ok}")

print(f"\n5. MATERIAL")
plate_a = deck_x * (mobo_y1 - mobo_y0)
cell_a  = 1.5*math.sqrt(3)*hex_r**2
removed = len(C) * cell_a
print(f"   plate {plate_a/100:.0f} cm2, {len(C)} cells remove {removed/100:.0f} cm2 = {100*removed/plate_a:.0f}%")
print(f"   at {mobo_deck_t} mm, PETG 1.27 g/cc: saves ~{removed*mobo_deck_t*1.27/1000:.0f} g of plate")
