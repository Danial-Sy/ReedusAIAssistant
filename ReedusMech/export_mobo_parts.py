"""Reliable mobo STL export, verifies wall files stay small (not full assembly)."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OPENSCAD = Path(r"C:\Program Files\OpenSCAD\openscad.exe")
SCAD = ROOT / "reedus_chassis.scad"
OUT = ROOT / "stl"
OUT.mkdir(exist_ok=True)

MAX_WALL_BYTES = 1_500_000
MAX_DECK_BYTES = 4_000_000
MAX_TRAY_BYTES = 2_500_000   # multi-part trays (pucks / rails / washers)

parts = {
    "mobodeck": MAX_DECK_BYTES,
    "mobowall_l": MAX_WALL_BYTES,
    "mobowall_r": MAX_WALL_BYTES,
    "mobopucks": MAX_TRAY_BYTES,   # REV4  : 8 epoxy standoff pucks (fallback)
    "moborails": MAX_TRAY_BYTES,   # REV4b : 3 T-slot rails  <- primary
    "moborails_L": MAX_TRAY_BYTES, # L-rail tray: flange on one side only
    "mobowashers": MAX_TRAY_BYTES, # REV4b : 10 spanner washers
}

failures = []

for part, max_bytes in parts.items():
    out = OUT / f"{part}.stl"
    if out.exists():
        out.unlink()
    cmd = [str(OPENSCAD), "-o", str(out), "-D", f'part="{part}"', str(SCAD)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    err = r.stderr or ""

    if "unknown part" in err.lower() or "ERROR:" in err:
        print(f"FAIL {part}: OpenSCAD error\n{err[-1500:]}")
        failures.append(part)
        continue
    if not out.exists() or out.stat().st_size < 1000:
        print(f"FAIL {part}: missing or tiny\n{err[-1500:]}")
        failures.append(part)
        continue

    size = out.stat().st_size
    if size > max_bytes:
        print(f"FAIL {part}: {size:,} > ceiling {max_bytes:,}, probably the "
              f"full assembly (Windows -D quoting), or the part grew a lot")
        out.unlink()
        failures.append(part)
        continue

    print(f"  ok  {part:14s} {size:>10,} bytes  ({100*size//max_bytes}% of ceiling)")

if failures:
    print(f"\n{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print(f"\nAll {len(parts)} mobo parts OK")
