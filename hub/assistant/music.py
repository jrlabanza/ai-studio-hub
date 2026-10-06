"""Music Studio assistant (YuE2): one call writes the title, the style line and tagged lyrics; the code checks the
shape. Only Music Studio's skill is loaded."""
from __future__ import annotations

import asyncio
import re
import time

import httpx

from . import runtime, skills as sk

SCHEMA = {"type": "object", "properties": {
    "title": {"type": "string"}, "style": {"type": "string"}, "lyrics": {"type": "string"},
    "instrumental": {"type": "boolean"}, "summary": {"type": "string"}},
    "required": ["title", "style", "lyrics", "instrumental", "summary"]}
INSTRUMENTAL = "[intro - instrumental]\n\n[verse - instrumental]\n\n[chorus - instrumental]\n\n[verse - instrumental]\n\n[chorus - instrumental]\n\n[outro - instrumental]"


TAGS = [(r"pre[- ]?chorus", "Pre-Chorus"), (r"chorus|ch|hook|refrain", "Chorus"), (r"verse|vs|v", "Verse"),
        (r"bridge|br", "Bridge"), (r"intro", "Intro"), (r"outro|end|ending", "Outro")]


def _canon(tag: str) -> str:
    inner = tag.strip("[] ").lower()
    if "instrumental" in inner:
        return f"[{inner}]"
    for pat, name in TAGS:
        if re.match(rf"^({pat})\b", inner):
            return f"[{name}]"
    return f"[{inner.title()}]"


def _tidy_lyrics(text: str) -> str:
    """Section tags on their own lines in YuE's names ([Verse], [Chorus]...), one blank line between sections."""
    t = text.replace("\\n", "\n").strip()
    t = re.sub(r"\[[^\]\n]{1,40}\]", lambda m: _canon(m.group(0)), t)
    t = re.sub(r"\s*(\[[^\]\n]{2,40}\])\s*", r"\n\n\1\n", t).strip()
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t if t.startswith("[") else f"[Verse]\n{t}"


async def plan(port: int, request: str, history: list[dict], prev: dict | None, device: str, image=None):
    inv = await runtime.studio_get(port, "/api/assistant/inventory", "Music Studio")
    skills = sk.load_skills("music")
    convo = "\n".join(f"{m['role']}: {m['text']}" for m in history[-6:])
    prev_txt = (f"\nThe current song (the user may be asking to change it): {prev['title']} | {prev['style']}\n{prev['lyrics'][:1500]}"
                if prev else "")
    t0 = time.time()
    msgs = [{"role": "system", "content": sk.general(skills)},
            {"role": "user", "content": f"{prev_txt}\n\nConversation:\n{convo}\n\nRequest: {request}\n\nWrite the song plan: "
                                        "title, style line, tagged lyrics (or instrumental), and a one-sentence summary. Answer in JSON."}]
    out, info = await runtime.chat(msgs, SCHEMA, device=device, keep=False, temperature=0.7)
    await runtime.unload()
    runtime.debug_dump("music", {"write": out})
    w = runtime.parse_json(out)
    said = f"{request} {convo}".lower()
    instrumental = bool(w.get("instrumental")) or bool(re.search(r"\b(instrumental|no vocals|without vocals|no singing|beat only)\b", said))
    style = " ".join(str(w.get("style") or "").split()).strip(" ,")
    if instrumental:
        rest = re.sub(r"(?i)^\s*instrumental\s*,?\s*(no vocals\s*,?)?", "", style).strip(" ,")
        if len(rest) < 8:                  # the genre went missing: take it from the request
            rest = re.sub(r"(?i)\b(a|an|the|make|create|song|track|music|please|instrumental)\b", " ", request)
            rest = ", ".join(x.strip() for x in re.split(r",| for | with ", " ".join(rest.split())) if x.strip())
        style = f"instrumental, no vocals, {rest}"
    lyrics = INSTRUMENTAL if instrumental else _tidy_lyrics(str(w.get("lyrics") or ""))
    notes = []
    if not instrumental and len(re.findall(r"^\[", lyrics, re.M)) < 3:
        notes.append("short lyrics - ask for more verses if the song should be longer")
    p = {"studio": "music", "title": " ".join(str(w.get("title") or "Untitled").split())[:80], "style": style,
         "lyrics": lyrics, "instrumental": instrumental, "takes": 1, "summary": str(w.get("summary") or "")[:300],
         "notes": notes, "voices": inv.get("voices") or []}
    return p, {"seconds": round(time.time() - t0, 1), "device": device, "calls": [info]}


async def render(port: int, p: dict, session: str, progress=None) -> dict:
    t0 = time.time()
    body = {"title": p["title"], "style": p["style"], "lyrics": p["lyrics"], "cot": "full", "takes": int(p.get("takes") or 1)}
    async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10)) as c:
        r = await runtime.studio_post(c, f"http://127.0.0.1:{port}/api/generate", json=body)
        if r.status_code != 200:
            raise RuntimeError(f"Music Studio: {r.text[:300]}")
        jobs = [j["id"] for j in (r.json().get("jobs") or [r.json()])]
        audios = []
        for jid in jobs:
            while True:
                await asyncio.sleep(4)
                j = (await c.get(f"http://127.0.0.1:{port}/api/jobs/{jid}")).json()
                st = j.get("stage") or {}
                if progress:
                    progress((st.get("label") if isinstance(st, dict) else st) or j.get("state"), 0)
                if j.get("state") in ("done", "failed", "error", "cancelled"):
                    break
            if j["state"] != "done":
                raise RuntimeError(f"Music Studio: {j.get('error') or j['state']}")
            song = (j.get("result") or {}).get("song_id") or jid
            audios.append(f"/media/music/{song}/audio.flac")
    return {"audios": audios, "seconds": round(time.time() - t0, 1), "info": f"YuE2 · {p['title']}"}
