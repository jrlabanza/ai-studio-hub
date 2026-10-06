"""Image Studio assistant (Qwen-Image 2.1): text to image, edit the conversation's latest image, transparent images.
Only Image Studio's skills are loaded."""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid

import httpx

from . import runtime, skills as sk

PICK = {"type": "object", "properties": {
    "mode": {"type": "string", "enum": ["t2i", "edit", "rgba"]},
    "aspect": {"type": "string", "enum": ["1:1", "4:3", "3:4", "3:2", "2:3", "16:9", "9:16"]},
    "count": {"type": "integer"}, "reason": {"type": "string"}}, "required": ["mode", "aspect", "count", "reason"]}
WRITE = {"type": "object", "properties": {"prompt": {"type": "string"}, "summary": {"type": "string"}},
         "required": ["prompt", "summary"]}


async def plan(port: int, request: str, history: list[dict], prev: dict | None, device: str, image: dict | None):
    inv = await runtime.studio_get(port, "/api/assistant/inventory", "Image Studio")
    if not inv.get("ready"):
        raise RuntimeError("Qwen-Image is not downloaded yet - open Image Studio's Models")
    skills = sk.load_skills("image")
    general = sk.general(skills)
    convo = "\n".join(f"{m['role']}: {m['text']}" for m in history[-6:])
    img_line = (f"An image is attached from this conversation ({image['label']}; it shows: {image.get('description') or 'unknown'})."
                if image else "No image is attached.")
    prev_txt = f"\nThe current plan (the user may be asking to change it): {prev['mode']}, {prev['aspect']}; prompt: {prev['prompt']}" if prev else ""
    t0 = time.time()
    m1 = [{"role": "system", "content": general},
          {"role": "user", "content": f"{img_line}{prev_txt}\n\nConversation:\n{convo}\n\nRequest: {request}\n\n"
                                      "Choose the mode, the orientation and how many images. Answer in JSON."}]
    o1, i1 = await runtime.chat(m1, PICK, device=device, keep=True)
    pick = runtime.parse_json(o1)
    said = f"{request} {convo}".lower()
    notes = []
    mode = pick.get("mode") if pick.get("mode") in ("t2i", "edit", "rgba") else "t2i"
    if mode == "edit" and not image:
        mode = "t2i"
        notes.append("no image in this conversation to edit - making a new one")
    if image and mode != "edit" and re.search(r"\b(edit|change|replace|make (it|her|him|the)|turn (it|this) into|add|remove|put)\b", request.lower()):
        mode = "edit"
    if re.search(r"\b(transparent|no background|without background|sticker|png with alpha)\b", said) and mode == "t2i":
        mode = "rgba"
    aspect = pick.get("aspect") if pick.get("aspect") in inv["aspects"] else "1:1"
    count = max(1, min(4, int(pick.get("count") or 1)))
    if mode == "edit":      # the previous prompt would be copied as a full re-description: give only the change
        m2 = [{"role": "system", "content": general + "\n\n" + sk.for_family(skills, "edit")},
              {"role": "user", "content": f"The picture to change: {image.get('description') or image['label']}.\n\nRequest: {request}\n\n"
                                          "Write ONE short edit instruction (at most two sentences) that names only the change and "
                                          "what to keep. Do not describe the picture. Summarise in one sentence. Answer in JSON."}]
    else:
        m2 = [{"role": "system", "content": general + "\n\n" + sk.for_family(skills, mode)},
              {"role": "user", "content": f"Mode: {mode}. {img_line}{prev_txt}\n\nConversation:\n{convo}\n\nRequest: {request}\n\n"
                                          "Write the prompt and summarise the plan in one sentence. Answer in JSON."}]
    o2, i2 = await runtime.chat(m2, WRITE, device=device, keep=False)
    await runtime.unload()
    w = runtime.parse_json(o2)
    runtime.debug_dump("image", {"pick": o1, "write": o2})
    prompt = " ".join(str(w.get("prompt") or request).split())
    prompt = re.sub(r",?\s*\b\d{1,2}:\d{1,2}\s*(aspect ratio|ratio|format)?\b", "", prompt, flags=re.I)   # layout words are settings
    if mode == "edit" and len(prompt.split()) > 45:      # still a re-description: the user's own words are the edit
        own = re.sub(r"(?i)^\s*(please\s+)?(edit|change)\s+(it|this|the (image|picture|poster))\s*[:,-]?\s*", "", request).strip()
        prompt = f"{own[0].upper() + own[1:] if own else request}; keep everything else the same."
        notes.append("used your words as the edit instruction")
    # printed text the user never asked for ("MATCHA FAIRY" on a sticker): drop the sentence that adds it
    if not re.search(r"[\"“'‘]|\b(text|says|saying|called|named|title|titled|logo|sign|label|caption|words?|lettering)\b", request.lower()):
        prompt = " ".join(sent for sent in re.split(r"(?<=[.!?])\s+", prompt)
                          if not re.search(r"[\"“'‘][A-Za-z][^\"”'’]{1,60}[\"”'’]", sent)) or prompt
    if mode == "rgba":     # no backgrounds in a transparent image
        prompt = re.sub(r"[^.,]*\b(background|backdrop|scenery|standing on|floor)\b[^.,]*[.,]?", "", prompt, flags=re.I)
        prompt = " ".join(prompt.split()).strip(" ,") or request
    p = {"studio": "image", "mode": mode, "aspect": aspect, "tier": inv.get("default_tier", "1K"), "tiers": inv.get("tiers", []),
         "aspects": inv["aspects"], "count": count, "steps": int(inv.get("steps") or 40), "prompt": prompt, "prompt_core": prompt,
         "image": image if mode == "edit" else None, "summary": str(w.get("summary") or "")[:300], "notes": notes,
         "reason": str(pick.get("reason") or "")[:300]}
    return p, {"seconds": round(time.time() - t0, 1), "device": device, "calls": [i1, i2]}


async def render(port: int, p: dict, out_dir, session: str, progress=None) -> dict:
    """Queue the job in Image Studio through its entrance, wait, and keep copies in the conversation (for "animate it")."""
    from pathlib import Path
    t0 = time.time()
    params = {"mode": p["mode"], "prompt": p["prompt"], "num_images": int(p["count"]), "steps": int(p["steps"])}
    if p["mode"] != "edit":
        from_tier = {"1K": 0.5, "1.5K": 0.75, "2K": 1.0}.get(p.get("tier", "1K"), 0.5)
        inv = await runtime.studio_get(port, "/api/status", "Image Studio")
        sizes = ((inv.get("presets") or {}).get("size_tiers") or {}).get(p.get("tier", "1K")) or {}
        wh = sizes.get(p["aspect"])
        if wh:
            params.update(width=int(wh[0]), height=int(wh[1]))
    files = {}
    if p["mode"] == "edit" and p.get("image"):
        files = {"images": (Path(p["image"]["path"]).name, Path(p["image"]["path"]).read_bytes(), "image/png")}
    async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10)) as c:
        r = await runtime.studio_post(c, f"http://127.0.0.1:{port}/api/generate", data={"params": json.dumps(params)}, files=files or None)
        if r.status_code != 200:
            raise RuntimeError(f"Image Studio: {r.text[:300]}")
        job_ids = r.json().get("job_ids") or [r.json().get("job_id")]
        urls = []
        for jid in job_ids:
            while True:
                await asyncio.sleep(3)
                j = (await c.get(f"http://127.0.0.1:{port}/api/jobs/{jid}")).json()
                pr = j.get("progress") or {}
                if progress:
                    progress(pr.get("message") or j.get("status"), (pr.get("percent") or 0) / 100)
                if j.get("status") in ("done", "error", "failed", "cancelled"):
                    break
            if j["status"] != "done":
                raise RuntimeError(f"Image Studio: {j.get('error') or j['status']}")
            for res in j.get("results") or []:
                data = (await c.get(f"http://127.0.0.1:{port}{res['url']}")).content
                folder = out_dir / session
                folder.mkdir(parents=True, exist_ok=True)
                name = f"{int(time.time())}-{uuid.uuid4().hex[:6]}.png"
                (folder / name).write_bytes(data)
                urls.append(f"/api/assistant/file/{session}/{name}")
    return {"images": urls, "seconds": round(time.time() - t0, 1), "info": f"Qwen-Image 2.1 · {p['mode']} · {p['aspect']}"}
