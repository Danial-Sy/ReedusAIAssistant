"""Z-height audit for Reedus tub, straps, electronics, deck clearance."""
import math

wheel_d = 80
motor_off = 6.5
motor_d = 37
body_z = wheel_d / 2 - motor_off
floor_top = 13
deck_z = 62
deck_t = 4
fuse_h = 36.5
bts_h = 15
lvc_h = 19
buck_h = 14
buck_z_off = 20
ecookie_h = 1.6
pico_stack = 11
strap_arch_h = 10
strap_clip_h = 11
strap_mount_z = -6
m3_head_h = 2.0
foam_t = 1.5
MIN_CLEAR = 4.0


def rotate_openscad(x, y, z):
    x1, y1, z1 = -y, x, z
    return x1, -z1, y1


def max_strap_world_z():
    od = motor_d + 8
    pts = []
    for a in range(0, 360, 10):
        r = math.radians(a)
        for z in [0, strap_arch_h, strap_clip_h]:
            pts.append(((od / 2) * math.cos(r), (od / 2) * math.sin(r), z))
    for xs in (-22, 22):
        pts.append((xs, 5, strap_arch_h / 2))
    return max(
        rotate_openscad(x, y, z + strap_mount_z)[2] + body_z for x, y, z in pts
    )


strap_crown = max_strap_world_z()
strap_foam = strap_crown + foam_t
strap_screw = strap_crown + m3_head_h

items = [
    ("Tub floor top", floor_top),
    ("Saddle block top", body_z + 2),
    ("Motor body top (horizontal Ø37)", body_z + motor_d / 2),
    ("Strap arch crown", strap_crown),
    ("Strap + 1.5 mm foam", strap_foam),
    ("Strap + M3 pan head", strap_screw),
    ("Fuse box top", floor_top + fuse_h),
    ("Buck stacked (+20 mm)", floor_top + buck_z_off + buck_h),
    ("LVC module top", floor_top + lvc_h),
    ("BTS7960 flat", floor_top + bts_h),
    ("Pico on ElectroCookie", floor_top + ecookie_h + pico_stack),
    ("--- DECK LIMIT ---", deck_z),
]

print("=== Z-HEIGHT AUDIT (mm) ===\n")
print(f"{'Component':<32} {'Top Z':>6}  {'Gap to deck':>11}")
print("-" * 55)
limiters = []
for name, ztop in items:
    if name.startswith("---"):
        print(f"\n{name:<32} {ztop:>6.0f}")
        continue
    gap = deck_z - ztop
    tag = ""
    if ztop <= deck_z and gap < MIN_CLEAR:
        tag = " *** FIX" if gap < 2 else " ** LOW"
        limiters.append((name, ztop, gap))
    print(f"{name:<32} {ztop:>6.1f}  {gap:>+9.1f} mm{tag}")

print(f"\nTub internal height: {deck_z - floor_top:.0f} mm")
print(f"Target minimum gap: {MIN_CLEAR:.0f} mm under deck (Z={deck_z})")

worst = min(limiters, key=lambda x: x[2], default=("none", 0, 99))
print(f"\nWorst case: {worst[0]}, {worst[2]:.1f} mm clearance")

if worst[2] >= MIN_CLEAR:
    print("VERDICT: PASS")
elif worst[2] >= 2:
    print("VERDICT: MARGINAL, usable but tight")
else:
    print("VERDICT: FAIL, will hit battery deck")

print(f"\nStrap params: arch_h={strap_arch_h}, clip={strap_clip_h}, mount_z={strap_mount_z}")
print("OLD straps (arch_h=12): crown ~Z56, screw ~Z58, too tall for deck_z=58")
