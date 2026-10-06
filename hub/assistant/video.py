"""Video Studio assistant: plain request -> plan (engine, mode, length, orientation, prompt in that engine's format)
-> render. Only Video Studio's own skills are loaded: the general one plus LTX-2.5 or MiniMax H3.

Call 1 picks the engine / mode / length / orientation, call 2 writes the prompt with only that engine's skill. The
code keeps it inside what is installed and what each engine can do. An image from this conversation (a Forge render)
can be the first frame: "animate it" -> image to video."""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

import httpx

from . import runtime, skills as sk

PICK_SCHEMA = {"type": "object", "properties": {
    "engine": {"type": "string", "enum": ["h3", "ltx25"]}, "mode": {"type": "string", "enum": ["t2v", "i2v"]},
    "seconds": {"type": "number"}, "aspect": {"type": "string", "enum": ["16:9", "9:16", "1:1"]},
    "reason": {"type": "string"}}, "required": ["engine", "mode", "seconds", "aspect", "reason"]}
WRITE_SCHEMA = {"type": "object", "properties": {"prompt": {"type": "string"}, "summary": {"type": "string"}},
                "required": ["prompt", "summary"]}
# MiniMax H3: the fields are written separately and the code builds the three-field format (a small model loses the
# layout when it has to put a multi-line format inside one JSON string)
H3_SCHEMA = {"type": "object", "properties": {
    "style": {"type": "string"}, "scene": {"type": "string"},
    "speech": {"type": "array", "items": {"type": "object", "properties": {
        "speaker": {"type": "string"}, "line": {"type": "string"}}, "required": ["speaker", "line"]}},
    "soundscape": {"type": "string"}, "music": {"type": "string"}, "summary": {"type": "string"}},
    "required": ["style", "scene", "speech", "soundscape", "music", "summary"]}


def _drop_quoted_speech(text: str, said: list[str]) -> str:
    """Speech belongs in the dialogue tags only. Cut "...and says 'Matcha time!'" out of a sentence (keeping its
    action), drop sentences that only echo a line ("The dragon responds with a loud 'No!'"), and drop leftover stubs."""
    if not said:
        return text
    q = r"[\"'‘“][^\"'’”]{1,200}?([.!?]?)[\"'’”]"
    verbs = r"(?:says|said|saying|replies|replying|responds|responding|shouts|shouting|asks|asking|exclaims|exclaiming|whispers|whispering)"
    def cut(m):            # end the sentence only when the quote ended it ("...says 'Hi!' She waves"), not mid-clause
        rest = m.string[m.end():].lstrip()
        return "." if m.group(1) and (not rest or rest[0].isupper()) else ""
    t = re.sub(r",?\s*(?:and\s+|then\s+)?" + verbs + r"\b[,:]?\s*" + q + r"(?:\s+in an? [a-z ,-]{2,40}?voice)?", cut, text, flags=re.I)
    keys = [l.strip(" .!?\"'").lower() for l in said if l.strip(" .!?\"'")]
    out = []
    for sent in re.split(r"(?<=[.!?])\s+", t):
        low = sent.lower()
        if any(re.search(r"[\"'‘“]\s*" + re.escape(k), low) for k in keys):
            continue                                      # only echoes a spoken line
        if len(re.findall(r"[a-z]+", low)) < 3:
            continue                                      # a stub left by the cut ("The knight.")
        out.append(sent)
    t = " ".join(out)
    t = re.sub(r"\s+([,.!?])", r"\1", t)
    t = re.sub(r"([.!?])[.!?]+", r"\1", t)
    return " ".join(t.split()).strip()


def _h3_build(w: dict, request: str) -> str:
    style = " ".join(str(w.get("style") or "").split()).rstrip(".")
    req = request.lower()
    if "cartoon" in req and "anime" not in req and "anime" in style.lower():
        style = re.sub(r"(?i)flat cel colou?r anime|anime", "cartoon", style)      # the user's word for the look wins
    scene = " ".join(str(w.get("scene") or request).split())
    lines = []
    for i, sp in enumerate((w.get("speech") or [])[:3]):
        line = " ".join(str(sp.get("line") or "").split()).strip('"\'')
        who = re.sub(r"\(\s*S\d+\s*\)|\bsays?:?\s*$", "", str(sp.get("speaker") or "The speaker"), flags=re.I)
        who = " ".join(who.split()).rstrip(".:,") or "The speaker"
        if line:
            lines.append(f"{who[0].upper() + who[1:]} (S{i + 1}) says: <d>[English] {line}</d>")
    spoken = [str(sp.get("line") or "") for sp in (w.get("speech") or [])[:3]]
    scene = _drop_quoted_speech(scene, spoken) or scene
    body = f"[Shot 1] {style + '. ' if style else ''}{scene}" + (" " + " ".join(lines) if lines else "")
    sound = " ".join(str(w.get("soundscape") or "Natural ambient sound that fits the scene.").split())
    for l in spoken:                       # the voices are in the dialogue; the soundscape keeps the other sounds
        if l.strip():
            sound = re.sub(re.escape(l.strip(" .!?")) + r"[.!?]*", "", sound, flags=re.I)
    sound = re.sub(r"\s*['\"‘“’”]{2}\s*", " ", sound).replace("  ", " ").strip() or "Natural ambient sound."
    music = " ".join(str(w.get("music") or "None.").split())
    return (f"integrated_multimodal_description: {body}\n\noverall_soundscape: {sound}\n\n"
            f"non_diegetic_music: {music if music.lower() not in ('', 'none') else 'None.'}")
H3_FIELDS = ("integrated_multimodal_description:", "overall_soundscape:", "non_diegetic_music:")


async def inventory(port: int) -> dict:
    async with httpx.AsyncClient(timeout=60) as c:
        for _ in range(90):
            r = await c.get(f"http://127.0.0.1:{port}/api/assistant/inventory")
            if r.status_code == 200:
                return r.json()
            await asyncio.sleep(3)
    raise RuntimeError("Video Studio did not answer - is it enabled in the hub?")


def _h3_shape(text: str, request: str) -> str:
    """Make sure an H3 prompt has its three fields (a small model sometimes drops one)."""
    t = text.strip()
    if not t.lower().startswith("integrated_multimodal_description:"):
        t = f"integrated_multimodal_description: [Shot 1] {t}"
    for f in H3_FIELDS[1:]:
        if f not in t:
            t += f"\n\n{f} " + ("Natural ambient sound that fits the scene." if f.startswith("overall") else "None.")
    t = re.sub(r"\s*(overall_soundscape:|non_diegetic_music:)", r"\n\n\1", t)
    return t.strip()


async def plan(port: int, request: str, history: list[dict], prev: dict | None, device: str,
               image: dict | None, force_engine: str | None = None) -> tuple[dict, dict]:
    inv = await inventory(port)
    engines = {k: v for k, v in inv["engines"].items() if v["ready"]}
    if not engines:
        raise RuntimeError("no video engine is installed - download LTX-2.5 or MiniMax H3 in Video Studio's Models")
    skills = sk.load_skills("video")
    general = sk.general(skills)
    convo = "\n".join(f"{m['role']}: {m['text']}" for m in history[-6:])
    eng_lines = "\n".join(f"- {k}: {v['name']} ({v['min_seconds']}-{v['max_seconds']} s)" for k, v in engines.items())
    img_line = (f"An image is attached from this conversation: {image['label']}. It shows (the tags it was made from): "
                f"{image.get('description') or 'unknown'}. Describe its subject only with these details - do not invent "
                "hair colours, clothes or places that are not listed." if image else "No image is attached.")
    prev_txt = (f"\nThe current plan (the user may be asking to change it): {prev['engine']}, {prev['mode']}, "
                f"{prev['seconds']} s, {prev['aspect']}; prompt: {prev['prompt'][:500]}") if prev else ""
    t0 = time.time()
    msg1 = [{"role": "system", "content": general},
            {"role": "user", "content": f"Installed engines:\n{eng_lines}\n{img_line}{prev_txt}\n\nConversation:\n{convo}\n\n"
                                        f"Request: {request}\n\nChoose the engine, the mode, the length in seconds and the "
                                        "orientation. Answer in JSON."}]
    out1, info1 = await runtime.chat(msg1, PICK_SCHEMA, device=device, keep=True)
    pick = runtime.parse_json(out1)
    notes = []
    said = f"{request} {convo}".lower()
    engine = str(pick.get("engine") or "")
    if "ltx" in said and "ltx25" in engines:
        engine = "ltx25"                                # the user's explicit choice wins
    elif re.search(r"\b(h3|minimax)\b", said) and "h3" in engines:
        engine = "h3"
    if force_engine in ("h3", "ltx25"):        # the Engine setting / the plan card's switch
        if force_engine in engines:
            engine = force_engine
        else:
            notes.append(f"{force_engine} is not installed")
    if engine not in engines:
        engine = "h3" if "h3" in engines else next(iter(engines))
        notes.append(f"using {engines[engine]['name']} (the other engine is not installed)")
    e = engines[engine]
    mode = "i2v" if (image and pick.get("mode") == "i2v") else "t2v"
    if image and mode == "t2v" and re.search(r"\b(animate|this (image|picture)|that (image|picture)|the (image|picture)|make (it|her|him|them) move|bring .* to life)\b", said):
        mode = "i2v"
    seconds = float(pick.get("seconds") or 5)
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:s|sec|secs|second|seconds)\b", request.lower())
    if m:
        seconds = float(m.group(1))                     # a length the user typed wins
    if seconds > e["max_seconds"] and engine == "h3" and "ltx25" in engines and force_engine != "h3" and not re.search(r"\b(h3|minimax)\b", said):
        engine, e = "ltx25", engines["ltx25"]
        notes.append(f"LTX-2.5: {seconds:g} s is longer than MiniMax H3's {engines['h3']['max_seconds']} s")
    if seconds > e["max_seconds"]:
        notes.append(f"{seconds:g} s is more than {e['name']} makes in one clip - set to {e['max_seconds']} s")
    seconds = max(e["min_seconds"], min(e["max_seconds"], seconds))
    aspect = pick.get("aspect") if pick.get("aspect") in ("16:9", "9:16", "1:1") else "16:9"
    if re.search(r"\b(vertical|portrait|tiktok|reels?|shorts|phone)\b", said):
        aspect = "9:16"
    # 2) the prompt, with only this engine's skill
    msg2 = [{"role": "system", "content": general + "\n\n" + sk.for_family(skills, engine)},
            {"role": "user", "content": f"Engine: {e['name']}. Mode: {'image to video' if mode == 'i2v' else 'text to video'}. "
                                        f"Length: {seconds:g} seconds. {img_line}{prev_txt}\n\nConversation:\n{convo}\n\n"
                                        f"Request: {request}\n\n" + (
                                            "Fill the fields: style = the look in the user's own words (cartoon, anime, "
                                            "live-action, claymation...) e.g. \"2D-animated cartoon\" or \"Live-action, "
                                            "cinematic\", with the shot size; scene = what is seen and "
                                            "happens, in order, with the camera move and the sounds of each action (no "
                                            "spoken words here); speech = only lines the user asked to be said, each with "
                                            "a short voice description of the speaker (e.g. \"the sleepy young woman\"); "
                                            "soundscape; music (or \"None.\"); summary = the plan in one sentence."
                                            if engine == "h3" else
                                            "Write the prompt in this engine's format and summarise the plan in one sentence.")
                                        + " Answer in JSON."}]
    out2, info2 = await runtime.chat(msg2, H3_SCHEMA if engine == "h3" else WRITE_SCHEMA, device=device, keep=False)
    await runtime.unload()
    w = runtime.parse_json(out2)
    runtime.debug_dump("video", {"pick": out1, "write": out2})
    if engine == "h3":
        prompt = _h3_build(w, request)
    else:
        prompt = " ".join(str(w.get("prompt") or request).replace("\n", " ").split())
    p = {"studio": "video", "engine": engine, "engine_name": e["name"], "mode": mode, "seconds": round(seconds, 1),
         "aspect": aspect, "size": e["default_size"], "sizes": e["sizes"], "prompt": prompt, "turbo": True,
         "engines": {k: v["name"] for k, v in engines.items()}, "image": image if mode == "i2v" else None, "summary": str(w.get("summary") or "")[:300],
         "reason": str(pick.get("reason") or "")[:300], "notes": notes}
    return p, {"seconds": round(time.time() - t0, 1), "device": device, "calls": [info1, info2]}


async def render(port: int, p: dict, session: str, progress=None) -> dict:
    """Queue the clip in Video Studio through its hub entrance (it gets the GPU) and wait for it."""
    t0 = time.time()
    async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10)) as c:
        first = None
        if p.get("mode") == "i2v" and p.get("image"):
            data = Path(p["image"]["path"]).read_bytes()
            r = await c.post(f"http://127.0.0.1:{port}/api/upload", files={"file": (Path(p["image"]["path"]).name, data, "image/png")})
            if r.status_code != 200:
                raise RuntimeError(f"Video Studio upload: {r.text[:300]}")
            first = r.json()["name"]
        body = {"model": p["engine"], "mode": "i2v" if first else "t2v", "prompt": p["prompt"], "aspect_ratio": p["aspect"],
                "size": p["size"], "duration": float(p["seconds"]), "first_image": first, "audio": True,
                "turbo": bool(p.get("turbo", True)), "speed_lora": "turbo", "enhance_prompt": False}
        for _ in range(60):
            r = await c.post(f"http://127.0.0.1:{port}/api/generate", json=body)
            if r.status_code != 503:
                break
            await asyncio.sleep(3)
        if r.status_code != 200:
            raise RuntimeError(f"Video Studio: {r.text[:400]}")
        job = r.json()
        while True:
            await asyncio.sleep(3)
            j = (await c.get(f"http://127.0.0.1:{port}/api/jobs/{job['id']}")).json()
            if progress:
                progress(j.get("stage") or j.get("status"), j.get("progress") or 0)
            if j.get("status") in ("succeeded", "failed", "cancelled"):
                break
    if j["status"] != "succeeded":
        raise RuntimeError(f"Video Studio: {j.get('error') or j['status']}")
    return {"videos": [f"/media/video/{j['output_file']}"], "job": j["id"], "seconds": round(time.time() - t0, 1),
            "info": f"{p['engine_name']} · {j.get('width')}×{j.get('height')} · {j.get('duration')} s"}
