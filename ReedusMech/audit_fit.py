"""Reedus chassis fit audit, axis-aligned bounding box checks."""
from dataclasses import dataclass

@dataclass
class Box:
    name: str
    cx: float
    cy: float
    l: float  # X
    w: float  # Y
    z0: float = 13
    h: float = 20

    @property
    def x0(self):
        return self.cx - self.l / 2

    @property
    def x1(self):
        return self.cx + self.l / 2

    @property
    def y0(self):
        return self.cy - self.w / 2

    @property
    def y1(self):
        return self.cy + self.w / 2

    @property
    def z1(self):
        return self.z0 + self.h

    def overlaps_xy(self, other: "Box", gap=0.0) -> bool:
        return not (
            self.x1 + gap <= other.x0
            or other.x1 + gap <= self.x0
            or self.y1 + gap <= other.y0
            or other.y1 + gap <= self.y0
        )

    def overlap_area(self, other: "Box") -> float:
        if not self.overlaps_xy(other):
            return 0
        dx = min(self.x1, other.x1) - max(self.x0, other.x0)
        dy = min(self.y1, other.y1) - max(self.y0, other.y0)
        return max(0, dx) * max(0, dy)


def layout(name, **kw):
    defaults = dict(z0=13, h=20)
    defaults.update(kw)
    return Box(name, **defaults)


# Current SCAD positions (pre-fix)
old = [
    layout("fuse", cx=0, cy=46, l=132, w=82, h=36.5),
    layout("ecookie", cx=0, cy=10, l=172.7, w=52.1, h=10),
    layout("bts_l", cx=-68, cy=-32, l=50, w=50, h=15),
    layout("bts_r", cx=68, cy=-32, l=50, w=50, h=15),
    layout("lvc", cx=-55, cy=-6, l=57, w=42, h=19),
    layout("buck", cx=55, cy=-6, l=45, w=21, h=14),
]

# Fixed layout (matches SCAD)
new = [
    layout("fuse", cx=0, cy=50, l=132, w=82, h=36.5),
    layout("ecookie", cx=0, cy=-33, l=172.7, w=52.1, h=10),
    layout("bts_l", cx=-60, cy=-85, l=50, w=50, h=15),
    layout("bts_r", cx=60, cy=-85, l=50, w=50, h=15),
    layout("lvc", cx=0, cy=-85, l=57, w=42, h=19, z0=13),
    layout("buck", cx=0, cy=-85, l=45, w=21, h=14, z0=33),
]

for label, boxes in [("OLD", old), ("FIXED", new)]:
    print(f"\n=== {label} LAYOUT ===")
    for i, a in enumerate(boxes):
        for b in boxes[i + 1 :]:
            xy = a.overlaps_xy(b)
            z = not (a.z1 <= b.z0 or b.z1 <= a.z0)
            if xy and z:
                print(f"  COLLISION: {a.name} vs {b.name}  area={a.overlap_area(b):.0f} mm²")
            elif xy and not z:
                print(f"  OK (stacked): {a.name} over {b.name}, same XY, different Z")

# Screw / fit constants
print("\n=== SCREW AUDIT ===")
checks = [
    ("Tub deck boss pilot (M3 self-tap)", 2.8, "2.6–2.9 PETG pilot"),
    ("Deck through-hole (M3)", 3.4, "M3 clearance"),
    ("Strap through-hole", 3.4, "M3 passes to 2.8 pilot boss"),
    ("Column foot / leg lug", 3.4, "M3×10 + nut"),
    ("Hex nut pocket AF", 7.0, "M3 nut ~5.5 mm AF"),
    ("Caster slot width", 4.6, "M3/M4 in slot"),
    ("Hub set-screw pilot", 2.8, "M3 grub self-tap"),
    ("Mobo standoff pilot", 2.8, "M3 brass standoff"),
]
for name, dia, note in checks:
    print(f"  {name}: Ø{dia}, {note}")

print("\n=== KEY CLEARANCES ===")
inner_w, bat_l, tray_wall, tol = 220, 181, 3, 0.4
bat_tray_notch_x_old = bat_l / 2 + tray_wall
bat_tray_notch_x_fix = inner_w / 2 - 2
print(f"  Battery tray notch X (fixed): ±{bat_tray_notch_x_fix:.1f}, matches tub bosses")

floor_top, deck_z, fuse_h = 13, 58, 36.5
print(f"  Tub internal height: {deck_z - floor_top} mm, fuse: {fuse_h} mm, headroom: {deck_z - floor_top - fuse_h:.1f} mm")

body_z, strap_clip = 33.5, 19.5
print(f"  Strap crown ~Z{body_z - 6 + strap_clip:.1f} vs deck ledge Z{deck_z} (need crown < deck)")
