"""Export Reedus printable STLs via OpenSCAD CLI."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OPENSCAD = Path(r"C:\Program Files\OpenSCAD\openscad.exe")
SCAD = ROOT / "reedus_chassis.scad"
OUT = ROOT / "stl"
OUT.mkdir(exist_ok=True)

for part in ["tub", "deck", "mobodeck", "mobowall_l", "mobowall_r", "hub", "tire", "spacer"]:
    out = OUT / f"{part}.stl"
    subprocess.run(
        [str(OPENSCAD), "-o", str(out), "-D", f'part="{part}"', str(SCAD)],
        check=True,
    )
    print(f"{part}: {out.stat().st_size:,} bytes")
