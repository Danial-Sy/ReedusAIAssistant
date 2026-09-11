#!/usr/bin/env python3
"""read the training run and say whether it went wrong."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from reedus_companion import config          # noqa: E402


def find_state(root: Path) -> Path | None:
    direct = root / "trainer_state.json"
    if direct.exists():
        return direct
    ckpts = sorted(root.glob("checkpoint-*/trainer_state.json"),
                   key=lambda p: int(p.parent.name.split("-")[1]))
    return ckpts[-1] if ckpts else None


def bar(v: float, lo: float, hi: float, width: int = 28) -> str:
    if hi <= lo:
        return ""
    n = int(round((v - lo) / (hi - lo) * width))
    return "#" * max(0, min(width, n))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(config.OUTPUT_DIR))
    args = ap.parse_args()
    root = Path(args.dir).expanduser()

    state_path = find_state(root)
    if not state_path:
        print(f"No trainer_state.json under {root}")
        return 1
    state = json.loads(state_path.read_text())
    hist = state.get("log_history", [])

    evals = [(h["step"], h["eval_loss"]) for h in hist if "eval_loss" in h]
    trains = [(h["step"], h["loss"]) for h in hist if "loss" in h and "eval_loss" not in h]
    if not evals:
        print("No eval entries, was eval_strategy set?")
        return 1

    lo = min(v for _, v in evals)
    hi = max(v for _, v in evals)
    best_step, best = min(evals, key=lambda kv: kv[1])

    print(f"\n{state_path.parent.name}, {len(evals)} evals over {state.get('global_step','?')} steps")
    print(f"{'step':>7}  {'eval':>7}  {'train':>7}")
    for step, ev in evals:
        near = [l for s, l in trains if abs(s - step) <= 25]
        tr = f"{near[-1]:.4f}" if near else "   -   "
        mark = "  <- best" if step == best_step else ""
        print(f"{step:>7}  {ev:.4f}  {tr:>7}  {bar(ev, lo, hi)}{mark}")

    first = evals[0][1]
    last = evals[-1][1]
    rise = last - best
    print(f"\n  first {first:.4f}   best {best:.4f} @ step {best_step}   last {last:.4f}")

    verdict = []
    if best >= first:
        verdict.append("FAIL  eval loss never improved on its starting point, the run learned "
                       "nothing useful.")
    if rise > 0.05:
        verdict.append(f"WARN  eval loss rose {rise:.3f} above best after step {best_step}, "
                       f"that is the v1 divergence signature. load_best_model_at_end means the "
                       f"saved adapter is the step-{best_step} one, so you are fine, but the "
                       f"epoch count is too high for this data.")
    if trains:
        tl = trains[-1][1]
        if last - tl > 0.35:
            verdict.append(f"WARN  train {tl:.3f} vs eval {last:.3f}, a gap that wide means it "
                           f"is memorising the oversampled seeds. Lower CONVO_REPEAT / "
                           f"IDENTITY_REPEAT before the next run.")
    if not verdict:
        verdict.append("OK    eval loss improved and stayed down. No divergence.")

    for v in verdict:
        print(f"\n  {v}")

    ck = sorted(root.glob("checkpoint-*"), key=lambda p: int(p.name.split("-")[1]))
    if ck:
        print(f"\n  checkpoints kept: {', '.join(c.name for c in ck)}")
    print(f"  adapter to evaluate: {root}")
    print("\n  Next: measure whether it fixed the thing it was for,")
    print("    python -m reedus_companion.scripts.eval_engagement --backend hf --label v3")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
