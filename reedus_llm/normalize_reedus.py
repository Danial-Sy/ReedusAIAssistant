#!/usr/bin/env python3
"""normalize a hand-written reedus dataset: load jsonl or json, tidy whitespace outside code blocks."""

import argparse, json, re, sys
from pathlib import Path
from typing import List, Dict, Any

FENCE_RE = re.compile(r"(```.*?```)", flags=re.DOTALL)

def load_conversations(path: Path) -> List[Dict[str, Any]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    # jsonl first
    conversations = []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    jsonl_success = True
    for ln in lines:
        try:
            obj = json.loads(ln)
            if isinstance(obj, dict) and "messages" in obj:
                conversations.append(obj)
            else:
                # not a conversation or not dict/jsonl
                jsonl_success = False
                conversations = []
                break
        except json.JSONDecodeError:
            jsonl_success = False
            conversations = []
            break
    if jsonl_success and conversations:
        return conversations

    # fallback: one json doc
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"[ERROR] Failed to parse input as JSON or JSONL: {e}", file=sys.stderr)
        sys.exit(1)

    if isinstance(obj, list):
        return [o for o in obj if isinstance(o, dict) and "messages" in o]
    if isinstance(obj, dict):
        for key in ("conversations", "data", "items", "records"):
            if key in obj and isinstance(obj[key], list):
                return [o for o in obj[key] if isinstance(o, dict) and "messages" in o]
        if "messages" in obj:
            return [obj]
    print("[ERROR] Could not find conversations list with 'messages' in input JSON.", file=sys.stderr)
    sys.exit(1)

def split_code_blocks(text: str):
    """Split into alternating [outside_code, codeblock, outside_code, codeblock, ...]."""
    return FENCE_RE.split(text)

def fix_hyphenation(s: str) -> str:
    s = re.sub(r"(\w)-\n(\w)", r"\1\2", s)
    return s

def normalize_outside_code(s: str, preserve_paragraphs: bool) -> str:
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    # 3+ newlines -> 2
    s = re.sub(r"\n{3,}", "\n\n", s)

    # mark paragraph breaks if preserving
    marker = "<<<PARA_BREAK>>>"
    if preserve_paragraphs:
        s = s.replace("\n\n", marker)

    # single newlines are line-wraps -> spaces
    s = s.replace("\n", " ")

    # collapse runs of spaces/tabs
    s = re.sub(r"[ \t]{2,}", " ", s)

    # restore paragraph breaks
    if preserve_paragraphs:
        s = s.replace(marker, "\n\n")

    # trim
    s = s.strip()
    return s

def normalize_content(content: str, preserve_paragraphs: bool, do_fix_hyphenation: bool) -> str:
    if not isinstance(content, str):
        content = "" if content is None else str(content)
    s = content.replace("\r\n", "\n").replace("\r", "\n")
    if do_fix_hyphenation:
        s = fix_hyphenation(s)
    parts = split_code_blocks(s)
    for i in range(0, len(parts), 2):  # even indices are outside code blocks
        parts[i] = normalize_outside_code(parts[i], preserve_paragraphs=preserve_paragraphs)
    return "".join(parts)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", type=Path, help="Input JSON or JSONL file (see formats above).")
    ap.add_argument("--out", type=Path, required=True, help="Output JSONL path.")
    ap.add_argument("--preserve-paragraphs", action="store_true", default=True,
                    help="Preserve paragraph breaks (double newlines). Default: on.")
    ap.add_argument("--no-preserve-paragraphs", dest="preserve_paragraphs", action="store_false",
                    help="Do NOT preserve paragraph breaks; everything becomes single-spaced.")
    ap.add_argument("--fix-hyphenation", action="store_true",
                    help="Fix PDF hyphenation across line breaks (word-\\nnext -> wordnext).")
    args = ap.parse_args()

    conversations = load_conversations(args.input)
    total_msgs = 0

    with args.out.open("w", encoding="utf-8") as fout:
        for conv in conversations:
            if not isinstance(conv, dict) or "messages" not in conv:
                continue
            msgs = conv.get("messages", [])
            if not isinstance(msgs, list):
                continue
            for m in msgs:
                if isinstance(m, dict) and "content" in m:
                    m["content"] = normalize_content(m["content"],
                                                     preserve_paragraphs=args.preserve_paragraphs,
                                                     do_fix_hyphenation=args.fix_hyphenation)
                    total_msgs += 1
            json.dump(conv, fout, ensure_ascii=False)
            fout.write("\n")

    print(f"[OK] Wrote {args.out} | conversations={len(conversations)} | messages={total_msgs}")

if __name__ == "__main__":
    main()
