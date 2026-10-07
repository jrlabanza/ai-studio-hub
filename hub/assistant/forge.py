"""Forge assistant: plain request -> plan (checkpoint, LoRAs, prompt, settings) -> render.

Two short model calls keep a small local model reliable: (1) pick the checkpoint from the installed list, (2) with
only that family's skill, write the prompt and choose LoRAs from a shortlist the code built (compatible with the
checkpoint's base, ranked by the request's words against names, tags and trigger words). The code then checks every
name, clamps weights, adds trigger words, and takes steps / CFG / sampler / size / quality tags from ModelProfile."""
from __future__ import annotations

import asyncio
import base64
import re
import time
import uuid
from pathlib import Path

import httpx

from ..config import DATA_DIR
from . import runtime, skills as sk

OUT_DIR = DATA_DIR / "assistant"
# which LoRA base models work on which checkpoint family
COMPAT = {
    "illustrious": {"illustrious", "noobai"},
    "pony": {"pony"},
    "animagine": {"sdxl 1.0", "sdxl"},
    "sdxl": {"sdxl 1.0", "sdxl"},
    "nl": set(),
    "sd15": {"sd 1.5"},
    "sd15_photo": {"sd 1.5"},
    "anima": {"anima"}, "anima_turbo": {"anima"}, "anima_38b": {"anima"},
}
STOP = set("a an the and or of with in on at to for from by this that it its is are be as make create draw generate image "
           "picture photo please style me my some very really into out up her his their them she he they".split())

PICK_SCHEMA = {"type": "object", "properties": {
    "checkpoint": {"type": "string"}, "orientation": {"type": "string", "enum": ["portrait", "landscape", "square"]},
    "count": {"type": "integer"}, "reason": {"type": "string"}}, "required": ["checkpoint", "orientation", "count", "reason"]}
WRITE_SCHEMA = {"type": "object", "properties": {
    "prompt": {"type": "string"}, "negative_extra": {"type": "string"},
    "loras": {"type": "array", "items": {"type": "object", "properties": {
        "name": {"type": "string"}, "weight": {"type": "number"}, "why": {"type": "string"}}, "required": ["name", "weight", "why"]}},
    "summary": {"type": "string"}}, "required": ["prompt", "negative_extra", "loras", "summary"]}


async def inventory(port: int) -> dict:
    async with httpx.AsyncClient(timeout=60) as c:
        for _ in range(90):           # Forge may be starting (the entrance answers 503 meanwhile)
            r = await c.get(f"http://127.0.0.1:{port}/studio-api/assistant/inventory")
            if r.status_code == 200:
                return r.json()
            await asyncio.sleep(3)
    raise RuntimeError("Forge did not answer - is it enabled in the hub?")


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 2 and w not in STOP]


def _compatible(lora: dict, family: str) -> bool:
    base = (lora.get("base") or "").lower()
    if family.startswith("anima"):        # Anima LoRAs only ("animagine", "animal", "animate" are not Anima)
        text = f"{base} {lora['name']} {lora.get('title') or ''}".lower()
        return bool(re.search(r"(?<![a-z])anima(?![a-z]|gine)", text))
    ok = COMPAT.get(family, set())
    return base in ok if ok else False


GENERIC = set("""girl girls boy boys woman man maiden lady moon sun star style styles anime manga character characters cute
outfit costume dress version ingame eyes hair with lora illustrious pony sdxl noobai xl il v1 v2 v3 v4 v5 alpha beta
original fanart art concept pose poses detail details slider realistic photo game impact series female male""".split())


def _is_character(l: dict) -> bool:
    """Tagged as a character, named char_*, or titled after a series that has 3+ character LoRAs ("Aria (Zenless
    Zone Zero) XL" - untagged character LoRAs are common on Civitai)."""
    tags = " ".join(l.get("tags") or []).lower()
    if "character" in tags or re.search(r"\bchar[-_ ]", l["name"].lower()):
        return True
    strong = {w for w in SERIES if w not in NOT_SERIES and not w.isdigit() and not re.fullmatch(r"(sd|v|ep?)\d+|\d+(st|nd|rd|th)", w)}
    return bool(strong & set(_words(f"{l.get('title') or ''} {l['name']}")))


# words that recur in LoRA names without naming a series (base models, file-name habits, common words)
NOT_SERIES = set("""illustrious illustriousxl pony ponyxl noob noobai sdxl xl sd15 lora loras locon lycoris char character
characters outfit outfits costume epoch all one blue red black white pink style concept pose v1 v2 v3 ill nai anime
nochekaiser""".split())


SERIES: set[str] = set()      # words shared by 3+ character LoRAs (genshin, impact, zzz...): a series, not a character


def _series_words(loras: list[dict]) -> set[str]:
    from collections import Counter
    c = Counter()
    for l in loras:
        if _is_character(l):
            c.update(set(_words(f"{l.get('title') or ''} {l['name']}")))
    return {w for w, n in c.items() if n >= 3}


COLOURS = set("""white black silver grey gray blue red green pink purple violet yellow orange brown golden gold blonde
cyan teal azure crimson scarlet navy dark light bright neon""".split())


def _char_match(l: dict, request: str) -> int:
    """How many distinctive words of the character's own name the request contains (0 = not named)."""
    q = set(_words(request))
    trig = (l.get("triggers") or [""])[0].split(",")[0]      # the trigger word itself, not the tag list after it
    own = re.split(r"\s[-|]\s|[(（\[]", l.get("title") or "")[0]     # "Hotaru Futaba (Fatal Fury: City of...)" -> the name
    special = set(_words(f"{own} {l['name']} {trig}")) - GENERIC - SERIES - COLOURS
    return len(q & {w for w in special if len(w) >= 4})


def _names_character(l: dict, request: str) -> bool:
    """A character LoRA only when the request names that character (Columbina, Anby) - not a generic word (maiden,
    girl, moon) and not just the series (Genshin)."""
    return _char_match(l, request) > 0


def shortlist(loras: list[dict], family: str, request: str, k: int = 12) -> list[dict]:
    q = set(_words(request))
    scored = []
    for l in loras:
        if not _compatible(l, family):
            continue
        if _is_character(l) and not _names_character(l, request):
            continue
        name_words = set(_words(l["name"] + " " + (l.get("title") or "")))
        tag_words = set(_words(" ".join(l.get("tags") or [])))
        trig_words = set(_words(" ".join(l.get("triggers") or [])))
        desc_words = set(_words(l.get("desc") or ""))
        s = 3 * len(q & name_words) + 2 * len(q & tag_words) + 2 * len(q & trig_words) + 0.5 * len(q & desc_words)
        if "style" in q and "style" in (tag_words | name_words):
            s += 1
        if s > 0:
            scored.append((s, l))
    scored.sort(key=lambda x: -x[0])
    return [l for _, l in scored[:k]]


def _size(ck: dict, orientation: str) -> tuple[int, int]:
    w, h = int(ck.get("width") or 1024), int(ck.get("height") or 1024)
    long_, short = max(w, h), min(w, h)
    if orientation == "square":
        side = int(round(((w * h) ** 0.5) / 64)) * 64
        return side, side
    if long_ == short:                      # the profile is square: make a 3:4-ish canvas of the same area
        long_, short = int(round(w * 1.15 / 64)) * 64, int(round(w * 0.87 / 64)) * 64
    return (short, long_) if orientation == "portrait" else (long_, short)


DEFAULT_ANIME = ("illustrious", "animagine", "pony", "anima")
ILLUSTRATED_FAMILY = {"illustrious": True, "animagine": True}      # families that draw flat anime by default
DEFAULT_PHOTO = ("sdxl", "sd15_photo", "nl")


def _defaults(inv: dict) -> tuple[str, str]:
    """The checkpoint marked (default for anime) and (default for photo): waiIllustrious-style first."""
    def first(fams, prefer=""):
        cands = [c for f in fams for c in inv["checkpoints"] if c["family"] == f]
        cands.sort(key=lambda c: (fams.index(c["family"]), prefer not in c["name"].lower()))
        return cands[0]["name"] if cands else ""
    return first(DEFAULT_ANIME, "wai"), first(DEFAULT_PHOTO)


def _ckpt_lines(inv: dict) -> str:
    anime, photo = _defaults(inv)
    def mark(c):
        return " (default for anime)" if c["name"] == anime else " (default for photo)" if c["name"] == photo else ""
    return "\n".join(f"- {c['name']}  [{c['family']}] {c['label']}{mark(c)}" for c in inv["checkpoints"] if c["family"] != "video")


def _fallback_checkpoint(inv: dict, request: str) -> dict:
    q = set(_words(request))
    want = ["sdxl", "sd15_photo"] if q & {"photo", "realistic", "photorealistic", "cinematic", "real"} else ["illustrious", "animagine", "anima"]
    for fam in want:
        for c in inv["checkpoints"]:
            if c["family"] == fam:
                return c
    return inv["checkpoints"][0]


async def plan(port: int, request: str, history: list[dict], prev: dict | None, device: str,
               prefer: str | None = None, only_named: str | None = None) -> tuple[dict, dict]:
    """The plan for `request` (with the conversation so far and the previous plan, for changes).

    prefer="anime" pins the default anime checkpoint (Auto productions: the key frame must match the look);
    only_named=<the user's own words> keeps only LoRAs of characters named there (no loosely matched concept LoRAs)."""
    inv = await inventory(port)
    SERIES.clear()
    SERIES.update(_series_words(inv["loras"]))
    skills = sk.load_skills("forge")
    general = sk.general(skills)
    convo = "\n".join(f"{m['role']}: {m['text']}" for m in history[-6:])
    prev_txt = ""
    if prev:
        prev_txt = (f"\nThe current plan (the user may be asking to change it): checkpoint {prev['checkpoint']}, "
                    f"LoRAs {[l['name'] for l in prev['loras']]}, prompt: {prev['prompt_core']}")
    t0 = time.time()
    # a character the user named and has a LoRA for decides which checkpoint families are possible
    names_in = only_named if only_named is not None else request + " " + convo
    named = sorted((l for l in inv["loras"] if _is_character(l) and _char_match(l, names_in)),
                   key=lambda l: -_char_match(l, names_in))
    hint = ""
    if named:
        top = named[0]
        hint = (f"\nThe user named a character you have a LoRA for: {top['title']} (made for {top['base']}). Choose a "
                f"checkpoint of a family that LoRA works on.")
    # 1) the checkpoint
    msg1 = [{"role": "system", "content": general},
            {"role": "user", "content": f"Installed checkpoints:\n{_ckpt_lines(inv)}\n{prev_txt}{hint}\n\nConversation:\n{convo}\n\n"
                                        f"Request: {request}\n\nChoose the checkpoint, the orientation and how many images "
                                        f"(1 unless the user asked for more, at most 4). Answer in JSON."}]
    out1, info1 = await runtime.chat(msg1, PICK_SCHEMA, device=device, keep=True)
    pick = runtime.parse_json(out1)
    by_name = {c["name"]: c for c in inv["checkpoints"]}
    by_name.update({c["title"]: c for c in inv["checkpoints"]})
    ck = by_name.get(str(pick.get("checkpoint", "")).strip()) or next(
        (c for c in inv["checkpoints"] if str(pick.get("checkpoint", "")).lower() in c["title"].lower() and pick.get("checkpoint")), None)
    notes = []
    if ck is None:
        ck = _fallback_checkpoint(inv, request)
        notes.append(f"picked {ck['name']} (the model's choice was not installed)")
    # a small model reads "anime" as "Anima": Anima / Animagine only when the user actually names them
    said = (request + " " + convo).lower()
    if ck["family"].startswith("anima") and not re.search(r"(?<![a-z])anima(?![a-z]|gine)", said) or \
            ck["family"] == "animagine" and "animagine" not in said:
        default_anime, _ = _defaults(inv)
        if default_anime and default_anime != ck["name"]:
            notes.append(f"anime → {default_anime} (write \"Anima\" or \"Animagine\" to use those)")
            ck = by_name[default_anime]
    if prefer == "anime":
        default_anime, _ = _defaults(inv)
        if default_anime and ck["name"] != default_anime and not ILLUSTRATED_FAMILY.get(ck["family"]):
            notes.append(f"{default_anime}: the default anime checkpoint suits this look")
            ck = by_name[default_anime]
    if named and not _compatible(named[0], ck["family"]):
        fits = [c for c in inv["checkpoints"] if _compatible(named[0], c["family"])]
        default_anime, _ = _defaults(inv)
        fits.sort(key=lambda c: c["name"] != default_anime)
        if fits:
            notes.append(f"used {fits[0]['name']}: the {named[0]['title']} LoRA is made for {named[0]['base']}")
            ck = fits[0]
    family = ck["family"]
    # 2) prompt + LoRAs, with only this family's skill
    short = shortlist(inv["loras"], family, request + " " + convo)
    lora_lines = "\n".join(f"- {l['name']} | {l['title']} | base {l['base']} | tags: {', '.join(l['tags'][:5])} | "
                           f"triggers: {' ; '.join(l['triggers'][:2])}" for l in short) or "(no matching LoRA installed)"
    msg2 = [{"role": "system", "content": general + "\n\n" + sk.for_family(skills, family)},
            {"role": "user", "content": f"Checkpoint: {ck['name']} - {ck['label']}.\nLoRA shortlist (compatible with it):\n"
                                        f"{lora_lines}\n{prev_txt}\n\nConversation:\n{convo}\n\nRequest: {request}\n\n"
                                        "Write the prompt in this family's format, pick LoRAs from the shortlist only (or "
                                        "none), and summarise the plan in one sentence. Answer in JSON."}]
    out2, info2 = await runtime.chat(msg2, WRITE_SCHEMA, device=device, keep=False)
    await runtime.unload()
    w = runtime.parse_json(out2)
    runtime.debug_dump("forge", {"pick": out1, "write": out2})
    # 3) check and complete it
    short_by = {l["name"]: l for l in short}
    all_by = {l["name"]: l for l in inv["loras"]}
    loras = []
    for item in (w.get("loras") or [])[:3]:
        name = str(item.get("name", "")).strip()
        l = short_by.get(name) or (all_by.get(name) if all_by.get(name) and _compatible(all_by[name], family) else None)
        if not l:
            if name:
                notes.append(f"skipped LoRA '{name}' (not installed or not for {family})")
            continue
        if _is_character(l) and not _names_character(l, request + " " + convo):
            notes.append(f"skipped character LoRA '{l['title']}' - you did not name that character")
            continue
        if only_named is not None and not (_is_character(l) and _names_character(l, only_named)):
            continue
        weight = max(0.2, min(1.2, float(item.get("weight") or 0.8)))
        loras.append({"name": l["name"], "title": l["title"], "weight": round(weight, 2), "why": str(item.get("why", ""))[:160],
                      "triggers": l["triggers"][:1]})
    prompt_core = " ".join(str(w.get("prompt") or request).split())
    prompt_core = re.sub(r"<lora:[^>]*>\s*,?\s*", "", prompt_core)      # LoRAs go in by name; final_prompt adds the tags
    # instruction words that a small model sometimes copies into the prompt
    junk = re.compile(r"(^|,)\s*(danbooru tags?|@artist(?: name)?|trigger words?|quality tags?|masterpiece|best quality)\s*(?=,|$)", re.I)
    prompt_core = junk.sub(r"\1", prompt_core)
    prompt_core = re.sub(r"\s*,(\s*,)+", ",", prompt_core).strip(" ,")
    # an empty scene: no character tags at all (a small model still writes "1girl, solo" out of habit)
    if re.search(r"\b(no (people|humans?|one|characters?|person)|nobody|empty (room|street|place|scene)|scenery only|without (people|anyone))\b", said) \
            and ck.get("danbooru"):
        person = re.compile(r"^(\d+\+?(girl|boy|other)s?|solo|solo focus|multiple (girls|boys)|looking at (viewer|another)|"
                            r"(upper|lower) body|cowboy shot|portrait|full body|.* hair|.* eyes|smile|open mouth|holding .*|"
                            r"standing|sitting|.* sleeves|.* coat|.* dress|.* shirt|.* skirt|blush)$", re.I)
        kept = [t.strip() for t in prompt_core.split(",") if t.strip() and not person.match(t.strip())]
        prompt_core = ", ".join(["no humans", "scenery"] + [t for t in kept if t.lower() not in ("no humans", "scenery")])
    # "no text", "no watermark" in a positive prompt draws them: move negations to the negative (keep real tags)
    keep_no = {"no humans", "no pupils", "no shoes", "no hat", "no bra", "no panties", "no headwear"}
    tags = [t.strip() for t in prompt_core.split(",")]
    moved = [t[3:].strip() for t in tags if t.lower().startswith("no ") and t.lower() not in keep_no]
    if moved and len(tags) > 3:
        prompt_core = ", ".join(t for t in tags if not (t.lower().startswith("no ") and t.lower() not in keep_no))
        w["negative_extra"] = ", ".join(x for x in [str(w.get("negative_extra") or "")] + moved if x)
    for l in loras:                        # the LoRA's first trigger word must be in the prompt
        trig = (l["triggers"] or [""])[0].split(",")[0].strip()
        if trig and trig.lower() not in prompt_core.lower():
            prompt_core = f"{trig}, {prompt_core}"
    orientation = str(pick.get("orientation") or "portrait")
    width, height = _size(ck, orientation)
    count = max(1, min(4, int(pick.get("count") or 1)))
    neg_extra = " ".join(str(w.get("negative_extra") or "").split())
    p = {"studio": "forge", "checkpoint": ck["name"], "checkpoint_title": ck["title"], "family": family, "label": ck["label"],
         "prompt_core": prompt_core, "quality": ck.get("quality") or "", "negative": ", ".join(x for x in (ck.get("negative"), neg_extra) if x),
         "loras": loras, "steps": ck["steps"], "cfg": ck["cfg"], "sampler": ck["sampler"], "width": width, "height": height,
         "count": count, "orientation": orientation, "summary": str(w.get("summary") or "")[:300],
         "reason": str(pick.get("reason") or "")[:300], "notes": notes, "shortlist": [l["name"] for l in short]}
    timing = {"seconds": round(time.time() - t0, 1), "device": device, "calls": [info1, info2]}
    return p, timing


def final_prompt(p: dict) -> str:
    parts = [p["prompt_core"]]
    q = (p.get("quality") or "").strip()
    if q and q.split(",")[0].strip().lower() not in p["prompt_core"].lower():
        parts.append(q)
    parts += [f"<lora:{l['name']}:{l['weight']}>" for l in p.get("loras") or []]
    return ", ".join(x for x in parts if x)


async def render(port: int, p: dict, session: str) -> dict:
    """Render the plan through Forge's hub entrance (so the hub hands Forge the GPU)."""
    payload = {"prompt": final_prompt(p), "negative_prompt": p.get("negative", ""), "steps": int(p["steps"]),
               "cfg_scale": float(p["cfg"]), "sampler_name": p["sampler"], "width": int(p["width"]), "height": int(p["height"]),
               "seed": int(p.get("seed", -1)), "batch_size": 1, "n_iter": int(p.get("count", 1)),
               "save_images": True, "send_images": True,
               "override_settings": {"sd_model_checkpoint": p["checkpoint_title"], "return_grid": False}, "override_settings_restore_afterwards": False}
    t0 = time.time()
    async with httpx.AsyncClient(timeout=httpx.Timeout(3600, connect=10)) as c:
        for _ in range(60):
            r = await c.post(f"http://127.0.0.1:{port}/sdapi/v1/txt2img", json=payload)
            if r.status_code != 503:
                break
            await asyncio.sleep(3)
    if r.status_code != 200:
        raise RuntimeError(f"Forge: {r.text[:400]}")
    d = r.json()
    folder = OUT_DIR / session
    folder.mkdir(parents=True, exist_ok=True)
    files = []
    for img in d.get("images") or []:
        name = f"{int(time.time())}-{uuid.uuid4().hex[:6]}.png"
        (folder / name).write_bytes(base64.b64decode(img.split(",", 1)[-1]))
        files.append(f"/api/assistant/file/{session}/{name}")
    info = d.get("info") or ""
    return {"images": files, "seconds": round(time.time() - t0, 1), "info": info[:4000], "prompt": payload["prompt"]}
