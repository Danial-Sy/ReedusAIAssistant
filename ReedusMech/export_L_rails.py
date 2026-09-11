import subprocess
from pathlib import Path

OS = Path(r"C:\Program Files\OpenSCAD\openscad.exe")
SCAD = Path(r"C:\ReedusFiles\Reedus\reedus_chassis.scad")
OUT = Path(r"C:\ReedusFiles\Reedus\stl")
OUT.mkdir(exist_ok=True)

for part in ["moborail_L", "moborails_L"]:
    out = OUT / f"{part}.stl"
    if out.exists():
        out.unlink()
    r = subprocess.run(
        [str(OS), "-o", str(out), "-D", f'part="{part}"', str(SCAD)],
        capture_output=True,
        text=True,
    )
    err = r.stderr or ""
    if "ERROR:" in err or "unknown part" in err.lower():
        print("FAIL", part)
        print(err[-800:])
        continue
    if not out.exists():
        print("FAIL missing", part)
        print(err[-800:])
        continue
    print(f"OK {part}: {out.stat().st_size:,} bytes")
