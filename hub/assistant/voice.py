"""Voice Studio assistant (Qwen3-TTS): one call writes the cast (saved voice / built-in speaker / designed voice) and
the lines; the code checks every voice against what is installed. Only Voice Studio's skill is loaded."""
from __future__ import annotations

import asyncio
import re
import time

import httpx

from . import runtime, skills as sk

SCHEMA = {"type": "object", "properties": {
    "cast": {"type": "array", "items": {"type": "object", "properties": {
        "name": {"type": "string"}, "kind": {"type": "string", "enum": ["library", "preset", "design"]},
        "voice": {"type": "string"}, "description": {"type": "string"}}, "required": ["name", "kind", "voice", "description"]}},
    "lines": {"type": "array", "items": {"type": "object", "properties": {
        "speaker": {"type": "string"}, "text": {"type": "string"}}, "required": ["speaker", "text"]}},
    "language": {"type": "string"}, "summary": {"type": "string"}},
    "required": ["cast", "lines", "language", "summary"]}


async def plan(port: int, request: str, history: list[dict], prev: dict | None, device: str, image=None):
    inv = await runtime.studio_get(port, "/api/assistant/inventory", "Voice Studio")
    skills = sk.load_skills("tts")
    convo = "\n".join(f"{m['role']}: {m['text']}" for m in history[-6:])
    presets = "\n".join(f"- {s['id']}: {s['name']}, {s['desc']} ({s['lang']})" for s in inv["speakers"])
    library = "\n".join(f"- {v['id']}: {v['name']} {v['desc']}" for v in inv["voices"]) or "(none saved)"
    prev_txt = (f"\nThe current plan (the user may be asking to change it): cast {[(c['name'], c['kind'], c.get('voice') or c.get('description')) for c in prev['cast']]}; "
                f"lines {[(l['speaker'], l['text']) for l in prev['lines']][:12]}") if prev else ""
    t0 = time.time()
    msgs = [{"role": "system", "content": sk.general(skills)},
            {"role": "user", "content": f"Built-in speakers:\n{presets}\nSaved voices:\n{library}\nLanguages: {', '.join(inv['languages'])}"
                                        f"{prev_txt}\n\nConversation:\n{convo}\n\nRequest: {request}\n\nPlan the cast and the lines. "
                                        "For kind preset or library put its id in voice; for design put the voice description in "
                                        "description. Answer in JSON."}]
    out, info = await runtime.chat(msgs, SCHEMA, device=device, keep=False, temperature=0.5)
    await runtime.unload()
    runtime.debug_dump("voice", {"write": out})
    w = runtime.parse_json(out)
    preset_ids = {s["id"]: s for s in inv["speakers"]}
    lib_ids = {v["id"]: v for v in inv["voices"]}
    notes, cast = [], []
    for c in (w.get("cast") or [])[:6]:
        name = " ".join(str(c.get("name") or "Narrator").split())[:40] or "Narrator"
        kind, voice, desc = c.get("kind"), str(c.get("voice") or "").strip().lower(), " ".join(str(c.get("description") or "").split())
        if kind == "library" and voice not in lib_ids:
            notes.append(f"no saved voice '{voice}' - designing one for {name}")
            kind = "design"
        if kind == "preset" and voice not in preset_ids:
            kind = "design"
        if kind == "preset":             # a built-in speaker only when the user named it - else design the described voice
            sp = preset_ids[voice]
            if not re.search(rf"\b({re.escape(voice)}|{re.escape(sp['name'].lower())})\b", f"{request} {convo}".lower()):
                kind = "design"
                said_voice = re.match(r"^\s*(.{4,120}?)\s*(?:says|said|saying|reads|reading|speaks|announces|whispers|:)", request, re.I)
                desc = desc or (said_voice.group(1).strip(" ,") if said_voice else f"a natural voice: {sp['desc'].lower()}")
        if name.lower() in preset_ids and kind == "design":
            name = "Narrator" if len(w.get("cast") or []) == 1 else name.title()
        asked = f"{request} {convo}".lower()
        for m in list(re.finditer(r"([A-Za-z-]+)\s+accent", desc)):          # no invented accents (a requested one stays)
            if m.group(1).lower() not in asked:
                desc = desc.replace(m.group(0), "").strip(" ,")
        for word in re.findall(r"\b(british|american|australian|irish|scottish|indian|filipino|japanese|korean|french|german|spanish|italian|southern|english)\b", asked):
            if word not in desc.lower():
                desc = f"{desc}, {word.title()} accent"                    # a requested accent is kept
        desc = re.sub(r",\s*,", ",", desc).strip(" ,")
        if kind == "design" and not desc:
            desc = "a clear, natural adult voice, medium pace"
        cast.append({"name": name, "kind": kind, "voice": voice if kind != "design" else "", "description": desc})
    if not cast:
        cast = [{"name": "Narrator", "kind": "design", "voice": "", "description": "a clear, warm adult narrator, medium pace"}]
    names = {c["name"].lower(): c["name"] for c in cast}
    lines = []
    for l in (w.get("lines") or [])[:60]:
        text = str(l.get("text") or "")
        directions = r"yawn|sigh|laugh|giggle|gasp|snor|mumbl|pause|whisper|cough|sniff|groan|chuckl|nod|shrug|beat|sob|cries|smil|grin|stretch|excited"
        def _direction(m):                 # *yawns* / [laughs] are directions (dropped); *matcha* is emphasis (kept)
            inner = m.group(1) or m.group(2) or m.group(3) or ""
            return " " if re.search(directions, inner, re.I) else f" {inner} "
        text = re.sub(r"\*([^*]{1,60})\*|\[([^\]]{1,40})\]|\(([^)]{1,40})\)", _direction, text)
        text = re.sub(r"(\.\.\.\s*){2,}", "... ", text)          # stage directions out, ellipsis runs shortened
        text = re.sub(r"\s+([,.!?…])", r"\1", " ".join(text.split())).strip()
        if text and text[-1] not in ".!?\"'”…":
            text += "."
        sents = re.split(r"(?<=[.!?])\s+", text)
        if len(sents) > 3:
            text = " ".join(sents[:3])
        if len(text) < 2:
            continue
        who = names.get(str(l.get("speaker") or "").lower()) or cast[0]["name"]
        lines.append({"speaker": who, "text": text})
    m = re.search(r"[\"“](.+?)[\"”]", request)
    if not lines and m:
        lines = [{"speaker": cast[0]["name"], "text": m.group(1)}]
    if not lines:
        raise RuntimeError("the assistant wrote no lines - say what should be spoken")
    lang = w.get("language") if w.get("language") in inv["languages"] else "Auto"
    p = {"studio": "tts", "cast": cast, "lines": lines, "language": lang, "summary": str(w.get("summary") or "")[:300],
         "notes": notes, "speakers": inv["speakers"], "voices": inv["voices"], "languages": inv["languages"]}
    return p, {"seconds": round(time.time() - t0, 1), "device": device, "calls": [info]}


async def render(port: int, p: dict, session: str, progress=None) -> dict:
    t0 = time.time()
    cast = {}
    for c in p["cast"]:
        if c["kind"] == "library":
            cast[c["name"]] = {"type": "voice", "voice_id": c["voice"]}
        elif c["kind"] == "preset":
            cast[c["name"]] = {"type": "preset", "speaker": c["voice"], "instruct": c.get("description", "")[:200] if c.get("description") else ""}
        else:
            cast[c["name"]] = {"type": "design", "instruct": c["description"]}
    body = {"lines": [{"speaker": l["speaker"], "text": l["text"]} for l in p["lines"]], "cast": cast, "language": p.get("language") or "Auto"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10)) as c:
        r = await runtime.studio_post(c, f"http://127.0.0.1:{port}/api/tts/dialogue", json=body)
        if r.status_code != 200:
            raise RuntimeError(f"Voice Studio: {r.text[:300]}")
        jid = r.json()["job_id"]
        while True:
            await asyncio.sleep(2)
            j = (await c.get(f"http://127.0.0.1:{port}/api/jobs/{jid}")).json()
            if progress:
                progress(j.get("message") or j.get("status"), j.get("progress") or 0)
            if j.get("status") in ("done", "error"):
                break
    if j["status"] != "done":
        raise RuntimeError(f"Voice Studio: {j.get('error') or 'failed'}")
    outs = (j.get("result") or {}).get("outputs") or []
    return {"audios": [f"/media/tts/{o['file']}" for o in outs], "seconds": round(time.time() - t0, 1),
            "info": f"Qwen3-TTS · {len(p['lines'])} line(s)"}
