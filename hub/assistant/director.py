"""Auto mode: decide which studio a request needs, or plan a production that chains studios.

route()  - one studio (Forge, Image, Music, Voice, Video) or a production. Clear words decide in code; the language
           model is asked only when the request is ambiguous.
plan()   - a music-video production: a key frame (Forge for anime / illustration, Image Studio otherwise), a song
           (Music Studio), then a Video Studio storyboard cut to the song's timed lyrics that opens on the key frame
           and keeps its character in the later shots (H3 references).
run()    - the steps in order, in the background, one studio on the GPU at a time (the hub's entrances hand it over).
           It stops after the key frame and after the song for the user's OK unless the plan says auto.

The video step is planned only when the song exists: the shots follow the karaoke timing of the chosen window
(the first chorus when the lyrics have one)."""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

import httpx

from . import forge, image, music, runtime

ROUTE_SCHEMA = {"type": "object", "properties": {
    "kind": {"type": "string", "enum": ["single", "production"]},
    "studio": {"type": "string", "enum": ["forge", "image", "music", "tts", "video"]},
    "reason": {"type": "string"}}, "required": ["kind", "studio", "reason"]}
DIRECT_SCHEMA = {"type": "object", "properties": {
    "title": {"type": "string"}, "look": {"type": "string"}, "character": {"type": "string"},
    "setting": {"type": "string"}, "keyframe": {"type": "string"}, "song": {"type": "string"},
    "seconds": {"type": "number"}, "summary": {"type": "string"}},
    "required": ["title", "look", "character", "setting", "keyframe", "song", "seconds", "summary"]}
SHOTS_SCHEMA = {"type": "object", "properties": {"shots": {"type": "array", "items": {"type": "object", "properties": {
    "action": {"type": "string"}, "character": {"type": "boolean"}}, "required": ["action", "character"]}}},
    "required": ["shots"]}

STUDIO_NAMES = {"forge": "Forge", "image": "Image Studio", "music": "Music Studio", "tts": "Voice Studio", "video": "Video Studio"}
PRODUCTION = re.compile(r"\b(opening|op|ending (theme|sequence)|music video|mv|lyric video|anime intro|intro sequence|"
                        r"theme song video|video (with|and|for) (a|its own|an original) (song|music|soundtrack))\b")
CUES = {
    "video": r"\b(video|clip|animate|animated|animation|footage|movie|film|cinematic shot|scene where|make (it|her|him|them) move)\b",
    "music": r"\b(song|music|track|beat|melody|jingle|instrumental|lyrics|sing|rap|anthem)\b",
    "tts": r"\b(say|says|saying|read (this|it)? ?aloud|narrat\w*|voice ?over|voice line|speak|pronounce|text to speech)\b",
    "image": r"\b(image|picture|photo|poster|logo|drawing|illustration|portrait|wallpaper|artwork|sticker|thumbnail|edit it)\b",
}
ILLUSTRATED = re.compile(r"\b(anime|manga|cartoon|illustrat\w*|2d|cel|chibi|pixel|waifu|danbooru|vtuber)\b")
QWEN_ONLY = re.compile(r"\b(poster|logo|typography|text|sign|banner|transparent|sticker|edit)\b")

DIRECT_SYSTEM = """You plan a short music video made by three local AI studios, in this order:
1. a KEY FRAME picture: the opening frame of the video, showing the main character in the setting;
2. a SONG made for it;
3. the VIDEO: shots cut to the song, opening on the key frame and keeping the same character.

Fill the fields:
- title: a short title for the song and the video.
- look: the visual style in a few words, in the user's words when they gave one (e.g. "cinematic 2D anime, flat cel colour, dramatic lighting").
- character: the main character's look in one sentence - age, hair, eyes, clothing, colours, props. Invent a fitting one when the user did not describe one. Empty only when the user wants no characters.
- setting: where it happens, in one sentence.
- keyframe: what the opening frame shows, as a plain picture request: the character, pose, setting, light, camera framing. Wide landscape framing, with the character's face clearly visible (facing the camera or a three-quarter view) - later shots copy the character from this picture.
- song: the song request for a songwriter: what it is about, the genre and mood, the language (English unless the user names one), male or female voice. For an anime opening or ending, unless the user describes another sound: an energetic J-rock / J-pop anime theme - fast tempo, driving drums and electric guitars, a soaring catchy chorus.
- seconds: the video length; 30 unless the user asks for another length (10 to 60).
- summary: the plan in one sentence.
Answer in JSON only."""

SHOTS_SYSTEM = """You write the shots of a music video for the MiniMax H3 video model. Each shot is listed with its time in the song and the lyrics sung over it. For every shot write:
- action: two or three sentences in playback order - the shot size (wide, medium, close-up), what is seen, what the main character does, and the camera movement. Refer to the main character only as <Subject 1>. No spoken words, no lyrics, no sound descriptions.
- character: true when <Subject 1> is in the shot, false for a shot of only scenery or objects.
Shot 1 starts from the key frame and brings it to life. Vary the shot sizes and camera moves from shot to shot, follow the mood of each lyric line, and keep the same look and setting throughout. Return exactly the number of shots listed. Answer in JSON only."""


# ------------------------------------------------------------------------------------------------------------- route
def route_rules(text: str, current: str | None) -> tuple[str | None, str | None, str]:
    """(kind, studio, reason) from clear words, or (None, None, "") when the language model should decide."""
    t = text.lower()
    if PRODUCTION.search(t) or (re.search(CUES["video"], t) and re.search(r"\b(song|soundtrack|original music)\b", t)):
        return "production", "video", "a music video: key frame, then song, then video"
    hits = [k for k, pat in CUES.items() if re.search(pat, t)]
    if len(hits) == 1:
        studio = hits[0]
        if studio == "image":
            studio = "image" if QWEN_ONLY.search(t) or current == "image" else "forge"
        if studio == "video" and "animate" in t:
            return "single", "video", "animate the last picture"
        return "single", studio, {"forge": "a picture", "image": "a picture", "music": "a song", "tts": "speech",
                                  "video": "a video clip"}[studio]
    if not hits and current:
        return "single", current, "a follow-up to the current plan"
    if not hits and ILLUSTRATED.search(t):
        return "single", "forge", "an illustrated picture"
    return None, None, ""


async def route(text: str, history: list[dict], current: str | None, device: str) -> tuple[str, str, str, dict | None]:
    kind, studio, reason = route_rules(text, current)
    if kind:
        return kind, studio, reason, None
    convo = "\n".join(f"{m['role']}: {m['text']}" for m in history[-4:])
    msgs = [{"role": "system", "content": "You route a request to one local AI studio: forge (anime and illustration pictures with "
                                          "Stable Diffusion models and LoRAs), image (photos, posters, logos, text in pictures, editing "
                                          "a picture), music (songs and instrumentals), tts (speech, narration, voices), video (video "
                                          "clips). kind = production only when the user wants a video together with its own song "
                                          "(a music video, an anime opening). Answer in JSON."},
            {"role": "user", "content": f"Current studio: {current or 'none'}\nConversation:\n{convo}\n\nRequest: {text}"}]
    out, info = await runtime.chat(msgs, ROUTE_SCHEMA, device=device, keep=True)
    d = runtime.parse_json(out)
    studio = d.get("studio") if d.get("studio") in STUDIO_NAMES else (current or "forge")
    kind = "production" if d.get("kind") == "production" else "single"
    return kind, studio, str(d.get("reason") or "")[:200], info


# -------------------------------------------------------------------------------------------------------------- plan
async def plan(ports: dict, text: str, history: list[dict], device: str) -> tuple[dict, dict]:
    t0 = time.time()
    msgs = [{"role": "system", "content": DIRECT_SYSTEM}, {"role": "user", "content": f"Request: {text}"}]
    out, info = await runtime.chat(msgs, DIRECT_SCHEMA, device=device, keep=True, temperature=0.5)
    d = runtime.parse_json(out)
    runtime.debug_dump("director", {"direct": out})
    look = " ".join(re.sub(r"\b\d+(\.\d+)?:\d+\b", "", str(d.get("look") or "cinematic")).split()).rstrip(". ,")
    character = " ".join(str(d.get("character") or "").split()).rstrip(".")
    setting = " ".join(str(d.get("setting") or "").split()).rstrip(".")
    seconds = float(d.get("seconds") or 30)
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:s|sec|secs|second|seconds)\b", text.lower())
    if m:
        seconds = float(m.group(1))
    seconds = max(10.0, min(60.0, seconds))
    said = f"{text} {look}".lower()
    pic_studio = "forge" if ILLUSTRATED.search(said) else "image"
    frame_req = f"{d.get('keyframe') or text}. {look}. Wide shot."
    if pic_studio == "forge":
        kp, kt = await forge.plan(ports["forge"], frame_req, [], None, device, prefer="anime", only_named=text)
        kp["count"] = 1
        kp["prompt_core"] = ", ".join(t for t in (x.strip() for x in kp["prompt_core"].split(","))
                                      if t and not re.search(r"\b(ratio|framing|aspect|landscape orientation)\b", t, re.I))
        if kp["height"] > kp["width"]:
            kp["width"], kp["height"] = kp["height"], kp["width"]
            kp["orientation"] = "landscape"
    else:
        kp, kt = await image.plan(ports["image"], frame_req, [], None, device, None)
        kp["count"] = 1
        if "16:9" in (kp.get("aspects") or []):
            kp["aspect"] = "16:9"
    song_req = (f"{d.get('song') or text}. It is the theme song of a short video titled \"{d.get('title') or ''}\". "
                "Keep it short, about one minute: an intro, one verse and one chorus.")
    mp, mt = await music.plan(ports["music"], song_req, [], None, device)
    sung = re.sub(r"\[[^\]]*\]", "", mp["lyrics"])
    for lang in ("japanese", "korean", "chinese", "mandarin", "cantonese"):        # "J-pop" is a sound, not a language
        if lang in mp["style"].lower() and lang not in text.lower() and sum(ord(ch) > 0x2e80 for ch in sung) < 10:
            mp["style"] = re.sub(rf"(?i)\b{lang}\b\s*,?\s*", "", mp["style"]).strip(" ,")
    await runtime.unload()
    p = {"studio": "production", "recipe": "music_video", "title": " ".join(str(d.get("title") or "Untitled").split())[:80],
         "look": look, "character": character, "setting": setting, "summary": str(d.get("summary") or "")[:300],
         "request": text, "auto": False, "status": "planned",
         "steps": [
             {"id": "keyframe", "studio": pic_studio, "title": "Key frame", "plan": kp, "status": "pending"},
             {"id": "song", "studio": "music", "title": "Song", "plan": mp, "status": "pending"},
             {"id": "video", "studio": "video", "title": "Video", "status": "pending",
              "plan": {"studio": "video", "engine": "h3", "seconds": seconds, "aspect": "16:9", "size": "fast", "turbo": True,
                       "window": "chorus", "shots": None}},
         ], "notes": []}
    timing = {"seconds": round(time.time() - t0, 1), "device": device, "calls": [info, *kt.get("calls", []), *mt.get("calls", [])]}
    return p, timing


# --------------------------------------------------------------------------------------------------------- the video
def shots_from_lyrics(lines: list[dict], start: float, end: float, max_s: float) -> list[dict]:
    """Shots for [start, end): every sung line runs to the next one, lines under a second ride on the next, neighbours
    merge while a shot stays within max_s, and a long stretch is split evenly (Video Studio's own rule, lyrics.ts)."""
    cues = [l for l in lines if l.get("text") and start - 0.05 <= float(l.get("start") or 0) < end]
    segs = [{"a": start, "b": end, "lines": []}] if not cues else []
    for i, c in enumerate(cues):
        segs.append({"a": start if i == 0 else float(c["start"]), "b": float(cues[i + 1]["start"]) if i + 1 < len(cues) else end,
                     "lines": [c["text"]]})
    for i in range(len(segs) - 2, -1, -1):
        if segs[i]["b"] - segs[i]["a"] < 1:
            segs[i + 1] = {"a": segs[i]["a"], "b": segs[i + 1]["b"], "lines": segs[i]["lines"] + segs[i + 1]["lines"]}
            del segs[i]
    merged: list[dict] = []
    for sg in segs:
        if merged and sg["b"] - merged[-1]["a"] <= max_s + 0.01:
            merged[-1]["b"] = sg["b"]
            merged[-1]["lines"] += sg["lines"]
        else:
            merged.append({**sg, "lines": list(sg["lines"])})
    out = []
    for m in merged:
        n = max(1, -(-int((m["b"] - m["a"]) * 1000) // int(max_s * 1000)))
        ln = (m["b"] - m["a"]) / n
        out += [{"start": round(m["a"] + k * ln, 3), "seconds": round(ln, 3), "lines": m["lines"]} for k in range(n)]
    return [o for o in out if o["seconds"] >= 2.0] or [{"start": start, "seconds": round(end - start, 3), "lines": []}]


def pick_window(lines: list[dict], duration: float, seconds: float, where: str) -> float:
    """Where the video starts in the song: the first chorus (a beat before its first line), else the first sung line."""
    seconds = min(seconds, duration)
    start = 0.0
    timed = [l for l in lines if l.get("text") and not l.get("estimated")] or [l for l in lines if l.get("text")]
    chorus = next((l for l in timed if str(l.get("section") or "").lower().startswith("chorus")), None)
    if where == "chorus" and chorus:
        start = max(0.0, float(chorus["start"]) - 0.5)
    elif where != "start" and timed:
        start = max(0.0, float(timed[0]["start"]) - 0.5)
    return round(max(0.0, min(start, duration - seconds)), 3)


def _sentence(t: str) -> str:
    t = " ".join(str(t or "").split()).strip()
    return t if not t or t[-1] in ".!?" else t + "."


def build_shot(i: int, action: str, with_char: bool, p: dict, song: dict) -> dict:
    """One storyboard shot in MiniMax H3's trained layout. Shot 1 opens on the key frame (image to video); a shot with
    the character uses it as <Picture 1> (reference generation) so it stays the same person; scenery is text to video."""
    look, char = p["look"], p["character"] or "the main character"
    music_line = f"The song \"{song['title']}\": {song['style']}."
    sound = "Only light ambient sound and soft whooshes of the camera moves under the song."
    act = _sentence(action)
    if i == 0 or not with_char:
        body = act.replace("<Subject 1>", char if i else "the character").replace("  ", " ")
        prompt = (f"integrated_multimodal_description: [Shot 1] The video is in {look} style. {body}\n\n"
                  f"overall_soundscape: {sound}\n\nnon_diegetic_music: {music_line}")
        return {"prompt": prompt, "cast": [], "first": i == 0}
    if "<Subject 1>" not in act:
        act = f"<Subject 1> is in the shot. {act}"
    first = re.split(r"(?<=[.!?])\s", act, maxsplit=1)[0]
    prompt = "\n".join([
        "subject_definitions:",
        f"<Subject 1> is the character in <Picture 1>: {char}; the face, hair, clothing and colours stay exactly as in "
        "<Picture 1>, and the background of <Picture 1> is not used.", "",
        "summary:", f"[reference generation] {first}", "",
        "retention_analysis:",
        "<Subject 1> (appears in [Shot 1]): fully_preserved - the face, hair, clothing and colours are kept; expression, "
        "pose and movement change naturally.", "",
        "detailed_description:", f"The target video is in {look} style. [Shot 1] {act}", "",
        "overall_soundscape:", sound, "",
        "non_diegetic_music:", music_line])
    return {"prompt": prompt, "cast": [1], "first": False}


async def plan_video(ports: dict, prod: dict, device: str) -> dict:
    """After the song: time its lyrics, choose the window, cut it into shots and write each shot."""
    vp = prod["steps"][2]["plan"]
    song = prod["steps"][1]["result"]
    lines, lrc = await karaoke(ports["music"], song["song_id"])
    duration = float(song.get("duration") or 0) or max([float(l.get("end") or l.get("start") or 0) for l in lines] + [vp["seconds"]])
    start = pick_window(lines, duration, float(vp["seconds"]), vp.get("window") or "chorus")
    end = min(duration, start + float(vp["seconds"]))
    shots = shots_from_lyrics(lines, start, end, 7.5)
    listing = "\n".join(f"Shot {k + 1} ({s['start'] - start:.1f}-{s['start'] - start + s['seconds']:.1f} s): "
                        + (f"sung: \"{' / '.join(s['lines'])}\"" if s["lines"] else "instrumental") for k, s in enumerate(shots))
    msgs = [{"role": "system", "content": SHOTS_SYSTEM},
            {"role": "user", "content": f"Title: {prod['title']}\nLook: {prod['look']}\n<Subject 1>: {prod['character'] or 'none'}\n"
                                        f"Setting: {prod['setting']}\nThe key frame (shot 1 starts from it): "
                                        f"{prod['steps'][0]['plan'].get('prompt_core') or prod['steps'][0]['plan'].get('prompt', '')}\n"
                                        f"Song style: {song['style']}\n\nShots:\n{listing}\n\nWrite {len(shots)} shots."}]
    out, info = await runtime.chat(msgs, SHOTS_SCHEMA, device=device, keep=False, temperature=0.6)
    await runtime.unload()
    runtime.debug_dump("director-shots", {"shots": out})
    written = (runtime.parse_json(out).get("shots") or [])
    built = []
    for k, s in enumerate(shots):
        w = written[k] if k < len(written) else {"action": f"<Subject 1> in {prod['setting'] or 'the setting'}, the camera slowly circles.", "character": True}
        action = str(w.get("action") or "")
        with_char = bool(w.get("character")) or "<Subject 1>" in action
        b = build_shot(k, action, with_char and bool(prod["character"]), prod, song)
        built.append({**b, "seconds": s["seconds"], "start": s["start"], "lyrics": " / ".join(s["lines"]), "action": action})
    vp.update(shots=built, audio_offset=start, lrc=lrc, song_seconds=round(duration, 2))
    return {"seconds": info.get("seconds"), "shots": len(built)}


# ------------------------------------------------------------------------------------------------------ studio calls
async def _poll(c: httpx.AsyncClient, url: str, done, step: dict, every: float = 3.0, label=None) -> dict:
    while True:
        await asyncio.sleep(every)
        j = (await c.get(url)).json()
        if label:
            step["progress"] = label(j)
        if done(j):
            return j


async def karaoke(port: int, song_id: str) -> tuple[list[dict], str]:
    """The song's timed lyrics from Music Studio's karaoke sync (run once; reused when it is already there)."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10)) as c:
        info = (await c.get(f"http://127.0.0.1:{port}/api/songs/{song_id}")).json()
        if not info.get("karaoke"):
            r = await runtime.studio_post(c, f"http://127.0.0.1:{port}/api/songs/{song_id}/karaoke")
            if r.status_code == 422:
                return [], ""                                    # instrumental: no lines to time
            if r.status_code != 200:
                raise RuntimeError(f"Music Studio karaoke: {r.text[:300]}")
            jid = r.json()["id"]
            j = await _poll(c, f"http://127.0.0.1:{port}/api/jobs/{jid}", lambda j: j.get("state") in ("done", "failed", "error", "cancelled"), {})
            if j.get("state") != "done":
                raise RuntimeError(f"Music Studio karaoke: {j.get('error') or j.get('state')}")
            info = (await c.get(f"http://127.0.0.1:{port}/api/songs/{song_id}")).json()
        k = info.get("karaoke") or {}
        data = (await c.get(f"http://127.0.0.1:{port}{k['json']}")).json() if k.get("json") else {}
        lrc = (await c.get(f"http://127.0.0.1:{port}{k['lrc']}")).text if k.get("lrc") else ""
    return data.get("lines") or [], lrc


async def render_video(ports: dict, prod: dict, step: dict) -> dict:
    vp = step["plan"]
    kf_path = Path(prod["steps"][0]["result"]["path"])
    song = prod["steps"][1]["result"]
    port = ports["video"]
    t0 = time.time()
    async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10)) as c:
        async def upload(name: str, data: bytes, mime: str) -> dict:
            r = await runtime.studio_post(c, f"http://127.0.0.1:{port}/api/upload", files={"file": (name, data, mime)})
            if r.status_code != 200:
                raise RuntimeError(f"Video Studio upload: {r.text[:300]}")
            return r.json()
        frame = (await upload(kf_path.name, kf_path.read_bytes(), "image/png"))["name"]
        # from the hub's media route (files on disk): asking Music Studio would start it just to serve one file
        ra = await c.get(f"http://127.0.0.1:{ports['hub']}/media/music/{song['song_id']}/audio.flac")
        if ra.status_code != 200 or len(ra.content) < 100_000:
            raise RuntimeError(f"could not read the song's audio ({ra.status_code}, {len(ra.content)} bytes)")
        audio = ra.content
        track = (await upload("song.flac", audio, "audio/flac"))["name"]
        shots = [{"prompt": s["prompt"], "seconds": s["seconds"], "image": frame if s.get("first") else None, "cut": True,
                  "cast": s["cast"], "lyrics": s.get("lyrics") or None} for s in vp["shots"]]
        body = {"model": vp.get("engine", "h3"), "mode": "storyboard", "prompt": "", "aspect_ratio": vp.get("aspect", "16:9"),
                "size": vp.get("size", "fast"), "duration": round(sum(s["seconds"] for s in shots), 2), "audio": True,
                "turbo": bool(vp.get("turbo", True)), "speed_lora": "turbo", "enhance_prompt": False,
                "character_images": [frame], "audio_track": track, "audio_offset": float(vp.get("audio_offset") or 0),
                "lyrics_lrc": vp.get("lrc") or None, "match_cuts": True, "shots": shots}
        r = await runtime.studio_post(c, f"http://127.0.0.1:{port}/api/generate", json=body)
        if r.status_code != 200:
            raise RuntimeError(f"Video Studio: {r.text[:400]}")
        job = r.json()
        j = await _poll(c, f"http://127.0.0.1:{port}/api/jobs/{job['id']}", lambda j: j.get("status") in ("succeeded", "failed", "cancelled"),
                        step, label=lambda j: {"stage": j.get("stage") or j.get("status"), "progress": j.get("progress") or 0})
    if j["status"] != "succeeded":
        raise RuntimeError(f"Video Studio: {j.get('error') or j['status']}")
    return {"videos": [f"/media/video/{j['output_file']}"], "job": j["id"], "seconds": round(time.time() - t0, 1),
            "info": f"MiniMax H3 storyboard · {len(shots)} shots · {j.get('width')}×{j.get('height')} · {j.get('duration')} s"}


# --------------------------------------------------------------------------------------------------------------- run
async def run(prod: dict, sid: str, ports: dict, device_fn, save) -> None:
    """Run the pending steps in order. Stops after a step for the user's OK unless prod["auto"]; a failed step stops
    the run and can be retried. Every change is saved, so a hub restart loses at most the step that was running."""
    prod["status"] = "running"
    save()
    try:
        for step in prod["steps"]:
            if step["status"] == "done":
                continue
            step.update(status="running", started=time.time(), error=None, progress=None)
            save()

            def progress(stage, frac, step=step):
                step["progress"] = {"stage": stage, "progress": frac}
            await runtime.unload()
            if step["id"] == "keyframe":
                p = step["plan"]
                if step["studio"] == "forge":
                    res = await forge.render(ports["forge"], p, sid)
                else:
                    res = await image.render(ports["image"], p, forge.OUT_DIR, sid, progress)
                res["path"] = str(forge.OUT_DIR / sid / res["images"][0].rsplit("/", 1)[-1])
            elif step["id"] == "song":
                res = await music.render(ports["music"], step["plan"], sid, progress)
                res["song_id"] = res["audios"][0].split("/")[3]
                res["title"], res["style"] = step["plan"]["title"], step["plan"]["style"]
                async with httpx.AsyncClient(timeout=30) as c:
                    info = (await c.get(f"http://127.0.0.1:{ports['music']}/api/songs/{res['song_id']}")).json()
                res["duration"] = info.get("duration") or info.get("seconds")
            else:
                if not step["plan"].get("shots"):
                    step["progress"] = {"stage": "timing the lyrics and writing the shots", "progress": 0}
                    save()
                    device, _ = await device_fn(None)
                    await plan_video(ports, prod, device)
                    save()
                    if not prod.get("auto"):                       # the shot list is worth a look before a long render
                        step.update(status="waiting", progress=None)
                        prod["status"] = "waiting"
                        save()
                        return
                res = await render_video(ports, prod, step)
            step.update(status="done", result=res, finished=time.time(), progress=None)
            save()
            if not prod.get("auto") and step is not prod["steps"][-1]:
                prod["status"] = "waiting"
                save()
                return
        prod["status"] = "done"
    except Exception as e:                                          # noqa: BLE001 - shown on the step
        running = next((s for s in prod["steps"] if s["status"] in ("running",)), None)
        if running:
            running.update(status="failed", error=str(e)[:600], progress=None)
        prod["status"] = "failed"
    finally:
        save()


def reset_from(prod: dict, step_id: str) -> None:
    """Re-roll a step: it and every step after it are planned / rendered again (the video depends on both)."""
    hit = False
    for s in prod["steps"]:
        if s["id"] == step_id:
            hit = True
        if hit:
            s.update(status="pending", result=None, error=None, progress=None)
            if s["id"] == "video":
                s["plan"]["shots"] = None
