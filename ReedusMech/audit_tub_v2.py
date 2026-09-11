"""REV 2 tub audit, every check derives from GROUND_TRUTH dims, not the SCAD."""

OK, BAD = "  ok ", "FAIL "
fails = []

def chk(name, cond, detail=""):
    print(f"{OK if cond else BAD}{name}  {detail}")
    if not cond:
        fails.append(name)

# --- structure constants (must mirror SCAD) ---
wall, floor_t, clr_gnd = 4, 4, 9
floor_z, floor_top = 9, 13
wheel_d = 80
axle_z = wheel_d / 2                      # 40
motor_off = 7
body_z = axle_z - motor_off               # 33
inner_hw = 110                            # inner cavity half width
half_w = 114
deck_z = 62
nose_y, tail_y = 96, -152
int_tail = tail_y + wall                  # -148 interior rear face

# --- motor ground truth ---
gb_d, gb_len = 37, 26.5
can_d, can_len = 32, 22
enc_d, enc_len = 32.6, 4.5
boss_d, boss_len, shaft_len = 12, 6, 15
putty = 3.0

# --- motor / cradle ---
print("== motor & cradle ==")
gb_x0, gb_x1 = inner_hw - gb_len, inner_hw          # 83.5..110
can_x0 = gb_x0 - can_len                             # 61.5
enc_x0 = can_x0 - enc_len                            # 57
cradle_x0, cradle_hw, cradle_top = 62, 24.5, body_z

chk("gearbox bore dia = 37 + 2*3 putty = 43", gb_d + 2 * putty == 43)
chk("can bore dia = 32 + 2*3 putty = 38", can_d + 2 * putty == 38)
chk("cradle clears encoder disc (block starts aft of can start)",
    cradle_x0 >= can_x0, f"block x0 {cradle_x0} vs can x0 {can_x0}")
chk("encoder hangs free of cradle", enc_x0 < cradle_x0)
chk("motor bottom above floor top (putty bed exists)",
    body_z - gb_d / 2 > floor_top, f"motor bottom {body_z - gb_d/2} vs floor {floor_top}")
chk("gearbox bore bottom cuts <=1.5mm into floor (putty bed, 2.5mm floor left)",
    0 <= floor_top - (body_z - (gb_d + 2 * putty) / 2) <= 1.5,
    f"bore bottom {body_z - (gb_d + 2*putty)/2}")
chk("cradle side wall >= 3mm around gearbox bore",
    cradle_hw - (gb_d + 2*putty) / 2 >= 3, f"{cradle_hw - (gb_d + 2*putty)/2}")
chk("boss Ø12 clears Ø26 opening by >=7mm radial", (26 - boss_d) / 2 >= 7)
chk("boss protrudes past outer wall", boss_len > wall, f"{boss_len} vs wall {wall}")
chk("shaft engagement in hub >= 13mm",
    boss_len + shaft_len - wall - 1 >= 13,   # wheel face at half_w+1 = 115
    f"{boss_len + shaft_len - wall - 1}")
chk("motor top clears deck", body_z + gb_d / 2 + 4 < deck_z,
    f"top+zip {body_z + 18.5 + 4} vs deck {deck_z}")
for zx, zone in [(70, (cradle_x0, gb_x0)), (96, (gb_x0, gb_x1))]:
    chk(f"zip station x={zx} within its zone", zone[0] < zx - 3.5 and zx + 3.5 < zone[1])

# --- caster ---
print("== caster ==")
caster_y, cc, spacer_h = -70, 40, 20 - floor_top
chk("spacer height levels ball at ground: floor_top + spacer - 20 == 0",
    floor_top + spacer_h - 20 == 0, f"spacer {spacer_h}mm")
chk("plate (50x35) fits floor: hole c-c 40 within plate 50", cc + 4 <= 50)
chk("dome hole Ø30 > ball Ø15 + dome, < plate width 35", 15 < 30 < 35)
chk("floor web between dome hole and bolt holes >= 3mm", 20 - 3.4/2 - 30/2 >= 3,
    f"web {20 - 1.7 - 15:.1f}mm")
chk("M3x16 length: plate2.2+spacer7+floor4 = 13.2 (+nut 2.4 = 15.6 <= 16)",
    2.2 + spacer_h + floor_t + 2.4 <= 16)
chk("nut stack under floor < ground clearance", 2.4 + 3 < clr_gnd)

# --- floor plan ---
print("== floor plan (AABB, gaps) ==")
class B:
    def __init__(s, n, cx, cy, lx, ly, z0=13, h=20):
        s.n, s.x0, s.x1 = n, cx - lx/2, cx + lx/2
        s.y0, s.y1, s.z0, s.z1 = cy - ly/2, cy + ly/2, z0, z0 + h

def gap(a, b):
    dx = max(a.x0 - b.x1, b.x0 - a.x1)
    dy = max(a.y0 - b.y1, b.y0 - a.y1)
    dz = max(a.z0 - b.z1, b.z0 - a.z1)
    return max(dx, dy, dz)

items = [
    B("bay_L",  -85, 0, 50, 49+6, h=38.5+4),  # cradle x -110..-62, hw 24.5
    B("bay_R",   85, 0, 50, 49+6, h=38.5+4),
    B("fuse",     0, 22, 82, 132, h=36.5),
    B("bts_L",  -75, 56, 50, 50, h=30),
    B("bts_R",   75, 56, 50, 50, h=30),
    B("lvc+buck",-65, -68, 57, 42, h=38),
    B("caster",   0, -70, 50, 35, h=9.2),
    B("ecookie",  0, -119, 172.7, 52.1, h=22),
    B("boss_FL", -108, 57, 11, 11, h=49),
    B("boss_FR",  108, 57, 11, 11, h=49),
    B("boss_RL", -108, -93, 11, 11, h=49),
    B("boss_RR",  108, -93, 11, 11, h=49),
]
MIN_GAP = 2.0
worst = []
for i in range(len(items)):
    for j in range(i + 1, len(items)):
        g = gap(items[i], items[j])
        if g < 25:
            worst.append((g, items[i].n, items[j].n))
worst.sort()
allok = True
for g, an, bn in worst:
    ok = g >= MIN_GAP
    allok &= ok
    print(f"{OK if ok else BAD}gap {an:9s} <-> {bn:9s} = {g:6.2f} mm")
chk("ALL pairwise gaps >= 2mm (most much larger)", allok)

WALL_MERGED = {"bay_L", "bay_R", "boss_FL", "boss_FR", "boss_RL", "boss_RR"}
for it in items:
    lim = inner_hw + (4 if it.n in WALL_MERGED else 0)
    chk(f"{it.n} inside cavity x", it.x0 >= -lim - 0.01 and it.x1 <= lim + 0.01,
        f"[{it.x0:.1f},{it.x1:.1f}]")
    chk(f"{it.n} inside cavity y", it.y0 >= int_tail and it.y1 <= 90,
        f"[{it.y0:.1f},{it.y1:.1f}]")

print("== headroom under deck (z=62) ==")
for n, top in [("fuse", 13+36.5), ("bts+heatsink", 13+30), ("lvc+buck stack", 13+38),
               ("ecookie+pico", 13+22)]:
    chk(f"{n} wire headroom >= 8mm", deck_z - top >= 8, f"{deck_z - top:.1f} mm free")

print("== bed fit ==")
chk("footprint 228 x 248 <= 250", max(228, nose_y - tail_y) <= 250,
    f"{nose_y - tail_y} long")

print()
print("RESULT:", "ALL CHECKS PASSED" if not fails else f"{len(fails)} FAILURES: {fails}")
