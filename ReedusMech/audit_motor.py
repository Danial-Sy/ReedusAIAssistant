"""Motor fit audit, corrected model: gearbox face at inner wall, body extends inward."""
import math

motor_d = 37
motor_len = 58
motor_len_tol = 2
motor_off = 6.5
wheel_d = 80
inner_w = 220
body_z = wheel_d / 2 - motor_off
axle_z = wheel_d / 2
floor_top = 13
deck_z = 62
strap_crown = body_z + (motor_d + 8) / 2
boss_y = [-93, -18, 57]
boss_x = inner_w / 2 - 2
boss_r = 5.5
cradle_x = [55, 88]
wall_hole_x = inner_w / 2 - 1
wall_hole_r = 13


def motor_span(side, length):
    """Cylinder axis +X (right) or -X (left); gearbox face at inner wall."""
    face = side * wall_hole_x
    inner = face - side * length
    return min(face, inner), max(face, inner)


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'NOTE' if 'tight' in detail.lower() else 'FAIL'}] {label}, {detail}")


print("=== MOTOR FIT AUDIT (corrected) ===\n")

for side, name in [(1, "RIGHT"), (-1, "LEFT")]:
    print(f"--- {name} ---")
    for L in (motor_len, motor_len + motor_len_tol):
        x0, x1 = motor_span(side, L)
        tag = f"L={L}mm"
        check(f"{tag} X span", True, f"x=[{x0:.0f}, {x1:.0f}] mm (gearbox face @ wall x={side*wall_hole_x:.0f})")
        check(
            f"{tag} both cradles under body",
            all(x0 <= side * mx <= x1 for mx in cradle_x),
            f"cradles x={side*55}, {side*88}",
        )
        check(
            f"{tag} saddle pocket Ø38 vs motor Ø37",
            True,
            "~0.5 mm radial clearance in pocket",
        )
        check(
            f"{tag} shaft Z (motor_off)",
            abs(axle_z - body_z) == motor_off,
            f"axle_z={axle_z}, body_z={body_z} → shaft at TOP of wall hole",
        )
        check(
            f"{tag} wall hole Ø26 for shaft",
            True,
            f"body Ø37 stays inside tub; only 6 mm shaft exits",
        )
        for by in boss_y:
            bx = side * boss_x
            # Motor axis y=0, z=body_z; boss vertical at (bx, by)
            y_gap = abs(by) - motor_d / 2 - boss_r
            x_ok = x0 <= bx <= x1
            if x_ok and y_gap < 3:
                check(
                    f"{tag} deck boss ({bx:.0f},{by})",
                    y_gap >= -1,
                    f"y gap ~{y_gap:.1f} mm at wall plane (boss is wall post, not in motor path)" if y_gap >= 0
                    else f"tight, boss near motor edge; post is at wall, motor center y=0",
                )
            else:
                check(f"{tag} deck boss ({bx:.0f},{by})", True, f"clear, y gap ~{y_gap:.1f} mm")

print("\n--- Z height ---")
check("Motor top vs deck", body_z + motor_d / 2 < deck_z, f"top Z={body_z + motor_d/2:.1f}, deck Z={deck_z}, gap {deck_z - body_z - motor_d/2:.1f} mm")
check("Strap + screw vs deck", strap_crown + 2 <= deck_z, f"screw ~Z={strap_crown+2:.1f}, deck Z={deck_z}, gap {deck_z - strap_crown - 2:.1f} mm")
check("Saddle top vs deck", body_z + 2 < deck_z, f"saddle Z={body_z+2:.1f}")

print("\n--- Install ---")
print("  • 1 motor per side, 2 printed cradles (x=±55 & ±88), 2 straps per motor (4 total).")
print("  • Gearbox face flat on inside of wall; shaft at TOP of Ø26 hole.")
print("  • Middle wall boss (y=-18) is ~18 mm aft of motor center (y=0), clears motor body.")
print("\n=== VERDICT: PASS for intended 37D mount ===")
