"""make reply text speakable: strip markdown, emoji, stage directions, split for pacing."""
from __future__ import annotations
import re

_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U00002B00-\U00002BFF\U00002190-\U000021FF\U0000FE00-\U0000FE0F\U0000200D]",
    flags=re.UNICODE,
)
_FENCED_CODE = re.compile(r"```.*?```", re.DOTALL)
_MD_LINK = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")        # [text](url) -> text
_BARE_URL = re.compile(r"https?://\S+|www\.\S+", re.I)
_BOLD_ITALIC = re.compile(r"(\*\*|__)(.+?)\1", re.DOTALL)  # **x** -> x
_STAGE = re.compile(r"(?<!\w)([*_])(?!\1)(.+?)\1(?!\w)", re.DOTALL)  # *laughs*
_LAUGHTER = re.compile(r"\b(laugh\w*|giggl\w*|chuckl\w*|snicker\w*|cackl\w*|lol|ha(?:ha)+|he(?:he)+)\b", re.I)
_INLINE_CODE = re.compile(r"`([^`]+)`")                    # `x` -> x


def _stage_sub(m: "re.Match") -> str:
    inner = m.group(2)
    if _LAUGHTER.search(inner):
        return ". haha. "
    return " "                                            # non-vocalizable, drop
_EMOTICON = re.compile(r"(?<!\w)[:;=8xX]['`-]?[)(\][DPpOo3/\\|]+(?!\w)")
_BULLET = re.compile(r"^[ \t]*(?:[-*+•]|\d+[.)])[ \t]+", re.M)
_HEADER = re.compile(r"^[ \t]*#{1,6}[ \t]*", re.M)
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([.!?,;:])")
_MULTISPACE = re.compile(r"[ \t]+")
_PUNCT_RUN = re.compile(r"[.!?…](?:\s*[.!?…])+")   # "?!." , ". ." , "..."


def _norm_punct_run(m: "re.Match") -> str:
    run = m.group(0).replace(" ", "")
    dots = run.count(".") + 3 * run.count("…")
    if set(run) <= {".", "…"}:
        return "..." if dots >= 3 else "."         # keep a trail-off
    return run.replace(".", "").replace("…", "")


_SEG_TOK = re.compile(r"([.!?…]+|\s+—\s+)")


def segment_for_pacing(text: str):
    """[(segment, kind)] so the synth can insert real pauses between thoughts."""
    if not text:
        return []
    pieces = _SEG_TOK.split(text)
    out = []
    i = 0
    while i < len(pieces):
        body = (pieces[i] or "").strip()
        delim = pieces[i + 1] if i + 1 < len(pieces) else ""
        i += 2
        if not body:
            continue
        d = delim.strip()
        if "…" in d or "..." in d:
            out.append((body + "...", "ellipsis"))
        elif d[:1] in ("!", "?"):
            out.append((body + d, "sentence"))
        elif d.startswith("."):
            out.append((body + ".", "sentence"))
        elif d == ", ":
            out.append((body, "soft"))
        else:
            out.append((body, "end"))
    return out


def apply_pronunciations(text: str, mapping: dict) -> str:
    """Whole-word respelling so the TTS says tricky names right."""
    if not text or not mapping:
        return text
    for word, say in mapping.items():
        text = re.sub(rf"\b{re.escape(word)}\b", say, text, flags=re.I)
    return text


def clean_for_speech(text: str) -> str:
    """Markdown/emoji/stage-direction reply -> clean spoken string."""
    if not text:
        return ""
    t = text
    t = _FENCED_CODE.sub(" ", t)
    t = _MD_LINK.sub(r"\1", t)
    t = _BARE_URL.sub(" ", t)
    t = _BOLD_ITALIC.sub(r"\2", t)
    t = _STAGE.sub(_stage_sub, t)       # *laughs* -> "haha", other *actions* dropped
    t = _INLINE_CODE.sub(r"\1", t)
    t = _EMOJI.sub(" ", t)
    t = _EMOTICON.sub(" ", t)
    t = _HEADER.sub("", t)
    t = _BULLET.sub("", t)
    t = t.replace("#", " ").replace("`", " ")
    t = re.sub(r"\n{2,}", ". ", t)
    t = t.replace("\n", " ")
    t = _MULTISPACE.sub(" ", t)
    t = _SPACE_BEFORE_PUNCT.sub(r"\1", t)
    t = _PUNCT_RUN.sub(_norm_punct_run, t)
    t = re.sub(r"^[\s.,;:—-]+", "", t)
    return t.rstrip(" -—:;,")


if __name__ == "__main__":
    samples = [
        "*laughs* yeah that's wild 😂 honestly **so** good",
        "Check it out: https://example.com/thing, it's `cool`.",
        "## Plan\n- step one\n- step two\n\nThat's it 🙂",
        "_sighs_ fine. :) we'll do it your way.",
        "Wait... are you serious right now?! *chuckles* okay, that's fair.",
    ]
    for s in samples:
        print(repr(s), "->", repr(clean_for_speech(s)))
