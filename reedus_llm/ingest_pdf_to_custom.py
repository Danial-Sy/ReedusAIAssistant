#!/usr/bin/env python3
# turn a chat transcript pdf into training pairs (legacy mistral pipeline)
import sys, re, json, os, argparse
from typing import List, Dict

# --- config, overridable by CLI ---
DEFAULT_PAIRS = int(os.environ.get("INGEST_PAIRS", "4"))         # max user→assistant pairs per sample
DEFAULT_PAIRS_NAME = int(os.environ.get("INGEST_PAIRS_NAME", "5"))  # max pairs if a name-intro appears

# pdf readers, best available wins
def _pdf_text(path: str) -> str:
    # Try pdfminer.six first
    try:
        from pdfminer.high_level import extract_text
        return extract_text(path)
    except Exception:
        pass
    # Fallback: PyPDF2
    try:
        import PyPDF2
        reader = PyPDF2.PdfReader(path)
        buf = []
        for p in reader.pages:
            try:
                buf.append(p.extract_text() or "")
            except Exception:
                pass
        return "\n".join(buf)
    except Exception:
        pass
    # Last resort: read as text (if already a .txt)
    with open(path, "rb") as f:
        return f.read().decode("utf-8", errors="ignore")

def _clean_line(s: str) -> str:
    # Normalize unicode spacing, collapse multiple spaces, strip
    s = s.replace("\u00a0", " ")  # non-breaking space
    s = re.sub(r"(\w)[-\n\r]+(\w)", r"\1\2", s)
    s = re.sub(r"[ \t]+", " ", s).strip()
    return s

def _finalize_messages(msgs: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Same cleanup as 2_normalize.py: keep only non-empty user/assistant turns."""
    out: List[Dict[str, str]] = []
    for m in msgs:
        role = (m.get("role") or "").strip()
        content = (m.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"] = (out[-1]["content"] + " " + content).strip()
        else:
            out.append({"role": role, "content": content})
    while out and out[0]["role"] != "user":
        out.pop(0)
    if out and out[-1]["role"] == "user":
        out.pop()
    return out

def _filter_min_pairs(ex) -> bool:
    roles = [m.get("role") for m in ex.get("messages", []) if isinstance(m, dict)]
    return ("user" in roles) and ("assistant" in roles)

# count user->assistant pairs in a turn list
def _count_pairs(dialog: List[Dict[str, str]]) -> int:
    c = 0
    for i in range(1, len(dialog)):
        if dialog[i-1]["role"] == "user" and dialog[i]["role"] == "assistant":
            c += 1
    return c

# did the user introduce themselves in this line
NAME_INTRO_PAT = re.compile(r"\b(i['’]m|i am|im|my name is)\s+[A-Z][A-Za-z0-9'’\-]+\b", re.IGNORECASE)

def parse_pdf_dialogs(pdf_path: str, max_pairs:int, max_pairs_name:int) -> List[Dict[str, List[Dict[str, str]]]]:
    text = _pdf_text(pdf_path)
    # Split into raw lines and pre-clean
    raw_lines = text.splitlines()
    lines = [_clean_line(l) for l in raw_lines]

    turn_pat = re.compile(r"^(user|assistant)\s*:\s*(.*)$", re.IGNORECASE)

    records: List[Dict[str, List[Dict[str, str]]]] = []
    cur_dialog: List[Dict[str, str]] = []
    cur_role = None
    name_intro_seen = False  # if we saw a user name-intro in this dialog

    def flush_dialog():
        nonlocal cur_dialog, name_intro_seen
        msgs = _finalize_messages(cur_dialog)
        if msgs:
            records.append({"messages": msgs})
        cur_dialog = []
        name_intro_seen = False

    for line in lines:
        if not line:
            continue

        m = turn_pat.match(line)
        if m:
            role = m.group(1).lower()
            content = m.group(2).strip()

            if role == "user" and cur_dialog and cur_dialog[-1]["role"] == "assistant":
                pairs_now = _count_pairs(cur_dialog)
                allowed = max_pairs_name if name_intro_seen else max_pairs
                if pairs_now >= allowed:
                    flush_dialog()

            cur_dialog.append({"role": role, "content": content})
            cur_role = role

            # update name-intro flag for user content we just appended
            if role == "user" and NAME_INTRO_PAT.search(content):
                name_intro_seen = True

        else:
            if cur_role and cur_dialog:
                cur_dialog[-1]["content"] = (cur_dialog[-1]["content"] + " " + line).strip()

    # flush last
    flush_dialog()

    # Filter for min pairs (has both roles)
    recs = [r for r in records if _filter_min_pairs(r)]
    return recs

def build_argparser():
    ap = argparse.ArgumentParser(description="Ingest a 'User:/Assistant:' PDF into a HF dataset with short multi-turn chunks.")
    ap.add_argument("pdf_path", help="Path to source PDF")
    ap.add_argument("save_dir", help="Output directory for HF dataset (arrow)")
    ap.add_argument("--pairs", type=int, default=DEFAULT_PAIRS, help=f"Max user→assistant pairs per sample (default: {DEFAULT_PAIRS})")
    ap.add_argument("--pairs-name", type=int, default=DEFAULT_PAIRS_NAME, help=f"Max pairs per sample if a user name-intro is seen (default: {DEFAULT_PAIRS_NAME})")
    return ap

def main():
    ap = build_argparser()
    args = ap.parse_args()

    pdf_path = args.pdf_path
    save_dir = args.save_dir

    try:
        from datasets import Dataset
    except Exception as e:
        print("This script requires `datasets`.\nInstall with: pip install datasets pdfminer.six PyPDF2", file=sys.stderr)
        raise

    recs = parse_pdf_dialogs(pdf_path, max_pairs=args.pairs, max_pairs_name=args.pairs_name)
    if not recs:
        print("No conversations parsed. Check that your PDF has 'User:' / 'Assistant:' prefixes.", file=sys.stderr)
        sys.exit(1)

    ds = Dataset.from_list(recs)

    # Save to disk in HF arrow format
    import shutil
    shutil.rmtree(save_dir, ignore_errors=True)
    ds.save_to_disk(save_dir)

    print(f"Saved {len(ds)} conversations to {save_dir}")
    # Lightweight peek
    try:
        print("Sample record:")
        print(json.dumps(ds[0], indent=2)[:800])
    except Exception:
        pass

if __name__ == "__main__":
    main()
