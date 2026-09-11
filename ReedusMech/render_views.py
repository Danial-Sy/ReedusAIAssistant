"""Render Reedus assembly views via OpenSCAD CLI."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OPENSCAD = Path(r"C:\Program Files\OpenSCAD\openscad.exe")
SCAD = ROOT / "reedus_chassis.scad"
OUT = ROOT / "renders"
OUT.mkdir(exist_ok=True)

VIEWS = [
    ("assembly", "reedus_assembly.png"),
    ("assembly_tub", "reedus_assembly_tub.png"),
    ("assembly_deck", "reedus_assembly_deck.png"),
    ("assembly_mobodeck", "reedus_assembly_mobodeck.png"),
]

for part, filename in VIEWS:
    out = OUT / filename
    cmd = [
        str(OPENSCAD),
        "-o",
        str(out),
        "--viewall",
        "--autocenter",
        "--imgsize=1600,1200",
        "--projection=p",
        "-D",
        f'part="{part}"',
        str(SCAD),
    ]
    print("Running:", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    print(result.stdout)
    if result.stderr:
        print(result.stderr)
    print(f"  -> {out.name}: {out.stat().st_size if out.exists() else 'MISSING'} bytes, exit {result.returncode}")
