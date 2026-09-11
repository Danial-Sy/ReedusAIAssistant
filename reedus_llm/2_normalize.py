# step 2: normalize every dataset into the same chat-turn shape (legacy mistral pipeline)
import os, shutil
from datasets import load_from_disk, Dataset
from collections import defaultdict

os.makedirs("data/normalized", exist_ok=True)

def filter_min_pairs(ex):
    roles = [m.get("role") for m in ex.get("messages", []) if isinstance(m, dict)]
    return ("user" in roles) and ("assistant" in roles)

def _finalize_messages(msgs):
    if not isinstance(msgs, list):
        return []
    out = []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
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

def _save_clean(ds, path):
    ds = ds.filter(filter_min_pairs)
    shutil.rmtree(path, ignore_errors=True)
    ds.save_to_disk(path)
    return ds

# --- persona ---
def norm_persona():
    ds = load_from_disk("data/raw_persona")
    records = []

    # already-chatty
    if hasattr(ds, "column_names") and "messages" in ds.column_names:
        for row in ds:
            msgs = _finalize_messages(row.get("messages") or [])
            if msgs:
                records.append({"messages": msgs})
        if not records:
            print(f"Warning: No records found for {ds}")
            return None
        out = Dataset.from_list(records)
        return _save_clean(out, "data/normalized/persona")

    # Persona-Chat style: utterances -> history + response
    for row in ds:
        history = row.get("history")
        if isinstance(history, list) and len(history) > 1:
            msgs = [{"role": "user" if i % 2 == 0 else "assistant", "content": str(t)} for i, t in enumerate(history)]
            msgs = _finalize_messages(msgs)
            if msgs:
                records.append({"messages": msgs})
            continue

        # simple pair
        msgs = []
        if row.get("human"):
            msgs.append({"role": "user", "content": str(row["human"])})
        if row.get("assistant"):
            msgs.append({"role": "assistant", "content": str(row["assistant"])})
        msgs = _finalize_messages(msgs)
        if msgs:
            records.append({"messages": msgs})
            continue

        # list of turns
        dialog = row.get("dialog") or row.get("dialogue")
        if isinstance(dialog, list) and dialog:
            msgs = [{"role": "user" if i % 2 == 0 else "assistant", "content": str(t)} for i, t in enumerate(dialog)]
            msgs = _finalize_messages(msgs)
            if msgs:
                records.append({"messages": msgs})
    if not records:
        print(f"Warning: No records found for {ds}")
        return None
    out = Dataset.from_list(records)
    return _save_clean(out, "data/normalized/persona")

# --- EmpatheticDialogues ---
def norm_empathetic():
    ds = load_from_disk("data/raw_empathetic")
    records = []

    # already-chatty
    if hasattr(ds, "column_names") and "messages" in ds.column_names:
        for row in ds:
            msgs = _finalize_messages(row.get("messages") or [])
            if msgs:
                records.append({"messages": msgs})
        if not records:
            print(f"Warning: No records found for {ds}")
            return None
        out = Dataset.from_list(records)
        return _save_clean(out, "data/normalized/empathetic")

    # row-per-utterance with conv_id, utterance_idx
    if hasattr(ds, "column_names") and "conv_id" in ds.column_names and "utterance" in ds.column_names:
        by_conv = defaultdict(list)
        for r in ds:
            cid = r.get("conv_id")
            utt = r.get("utterance")
            idx = r.get("utterance_idx", 0)
            if cid is not None and isinstance(utt, str):
                by_conv[cid].append((idx, utt))
        for cid, items in by_conv.items():
            items.sort(key=lambda x: x[0])
            dialog = [u for _, u in items]
            msgs = [{"role": "user" if i % 2 == 0 else "assistant", "content": u} for i, u in enumerate(dialog)]
            msgs = _finalize_messages(msgs)
            if msgs:
                records.append({"messages": msgs})
        if not records:
            print(f"Warning: No records found for {ds}")
            return None
        out = Dataset.from_list(records)
        return _save_clean(out, "data/normalized/empathetic")

    # conversation list with role/from and value/content
    for row in ds:
        conv = row.get("conversations") or row.get("conversation")
        msgs = []
        if isinstance(conv, list):
            for m in conv:
                src = (m.get("from", "") or m.get("role", "")).lower()
                val = m.get("value") or m.get("content") or ""
                if not val:
                    continue
                if src in ("human", "user"):
                    msgs.append({"role": "user", "content": val})
                elif src in ("gpt", "assistant", "bot"):
                    msgs.append({"role": "assistant", "content": val})
        msgs = _finalize_messages(msgs)
        if msgs:
            records.append({"messages": msgs})
    if not records:
        print(f"Warning: No records found for {ds}")
        return None
    out = Dataset.from_list(records)
    return _save_clean(out, "data/normalized/empathetic")

# --- skill / BST-like ---
def norm_skill():
    ds = load_from_disk("data/raw_skill")
    records = []

    for row in ds:
        # already-chatty
        msgs = row.get("messages")
        if isinstance(msgs, list):
            msgs = _finalize_messages(msgs)
            if msgs:
                records.append({"messages": msgs})
            continue

        dlg = row.get("dialogue")
        if isinstance(dlg, list) and dlg and isinstance(dlg[0], dict):
            tmp = []
            for i, turn in enumerate(sorted(dlg, key=lambda t: t.get("utterance_idx", i))):
                text = turn.get("utterance") or turn.get("text") or turn.get("value")
                spk = (turn.get("speaker") or turn.get("role") or "").lower()
                if not text:
                    continue
                if spk in ("user", "human", "prompter"):
                    tmp.append({"role": "user", "content": str(text)})
                elif spk in ("assistant", "gpt", "bot"):
                    tmp.append({"role": "assistant", "content": str(text)})
                else:
                    tmp.append({"role": "user" if len(tmp) % 2 == 0 else "assistant", "content": str(text)})
            tmp = _finalize_messages(tmp)
            if tmp:
                records.append({"messages": tmp})
            continue

        def _as_list(x):
            if x is None:
                return []
            if isinstance(x, list):
                return [str(t).strip() for t in x if str(t).strip()]
            if isinstance(x, str):
                s = x.strip()
                if not s:
                    return []
                for delim in ["|||", "__eou__", "\n", "|"]:
                    if delim in s:
                        return [p.strip() for p in s.split(delim) if p.strip()]
                return [s]
            return []

        prev = _as_list(row.get("previous_utterance"))
        free = _as_list(row.get("free_messages"))
        guided = _as_list(row.get("guided_messages"))
        sugg = _as_list(row.get("suggestions")) or _as_list(row.get("guided_chosen_suggestions"))

        if prev or free or guided or sugg:
            dialog = prev[:]
            # interleave free with guided+sugg
            tail = guided + sugg
            i = 0
            while i < len(free) or i < len(tail):
                if i < len(free): dialog.append(free[i])
                if i < len(tail): dialog.append(tail[i])
                i += 1
            tmp = [{"role": "user" if i % 2 == 0 else "assistant", "content": t} for i, t in enumerate(dialog)]
            tmp = _finalize_messages(tmp)
            if tmp:
                records.append({"messages": tmp})
            continue

        # context-only or Q/A fallback
        pieces = []
        ctx = row.get("context")
        if isinstance(ctx, list):
            pieces.extend([c for c in ctx if isinstance(c, str)])
        elif isinstance(ctx, str) and ctx.strip():
            pieces.append(ctx.strip())
        if pieces:
            tmp = [{"role": "user" if i % 2 == 0 else "assistant", "content": t} for i, t in enumerate(pieces)]
            tmp = _finalize_messages(tmp)
            if tmp:
                records.append({"messages": tmp})
            continue

        u = row.get("question") or row.get("prompt") or row.get("system_prompt") or ""
        a = row.get("response") or row.get("answer") or ""
        tmp = []
        if u:
            tmp.append({"role": "user", "content": str(u)})
        if a:
            tmp.append({"role": "assistant", "content": str(a)})
        tmp = _finalize_messages(tmp)
        if tmp:
            records.append({"messages": tmp})
    if not records:
        print(f"Warning: No records found for {ds}")
        return None
    out = Dataset.from_list(records)
    return _save_clean(out, "data/normalized/skill")

# --- DailyDialog ---
def norm_daily():
    ds = load_from_disk("data/raw_daily")
    records = []
    for row in ds:
        dialog = row.get("dialog") or row.get("dialogue")
        if isinstance(dialog, list) and dialog:
            msgs = [{"role": "user" if i % 2 == 0 else "assistant", "content": str(t)} for i, t in enumerate(dialog)]
            msgs = _finalize_messages(msgs)
            if msgs:
                records.append({"messages": msgs})
    if not records:
        print(f"Warning: No records found for {ds}")
        return None
    out = Dataset.from_list(records)
    return _save_clean(out, "data/normalized/daily")

# --- SODA ---
def norm_soda():
    ds = load_from_disk("data/raw_soda")
    records = []
    for row in ds:
        dialog = row.get("dialogue") or row.get("dialog")
        if isinstance(dialog, list) and dialog:
            msgs = [{"role": "user" if i % 2 == 0 else "assistant", "content": str(t)} for i, t in enumerate(dialog)]
            msgs = _finalize_messages(msgs)
            if msgs:
                records.append({"messages": msgs})
    if not records:
        print(f"Warning: No records found for {ds}")
        return None
    out = Dataset.from_list(records)
    return _save_clean(out, "data/normalized/soda")

# --- main ---
if __name__ == "__main__":
    p = norm_persona()
    e = norm_empathetic()
    k = norm_skill()
    d = norm_daily()
    z = norm_soda()

    print("Normalized:")
    print(" Persona     :", len(p))
    print(" Empathetic  :", len(e))
    print(" Skill/BST   :", len(k))
    print(" DailyDialog :", len(d))
    print(" SODA        :", len(z))
