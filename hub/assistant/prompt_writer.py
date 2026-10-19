"""Video prompt writer: MiniMax H3 storyboard prompts, written and revised in the Assistant.

The format and rules are the minimax-video-prompt skill (subject_definitions / summary / retention_analysis /
detailed_description), summarised in RULES so the system prompt stays a few hundred tokens inside the local model's
8k context, plus what the user learned on H3 storyboards:

* "hard cut" and "locked off" in the Opening paragraph (the code always writes that paragraph);
* in a solo shot H3 lip-syncs the visible character, so a line spoken off screen is never put in the prompt - it
  becomes a separate voice-over (the code moves it and says so in the part's notes);
* a voice's gender / age does not follow a text description: every speaker gets a voice reference slot;
* name only what IS in frame (unused reference slots are declared "not used", without naming who they show);
* every shot gives the background where the camera actually is.

Two kinds of call, both JSON: one plans the whole set (cast, reference slots, the parts), then one per part writes
its beats and paragraphs; the code assembles the four sections, so the layout and the fixed slot order never drift.
A revision re-plans with the previous plan and rewrites only the parts that changed (or all of them when the cast,
the locations or the look changed); untouched parts keep their text, including edits made in the panel.
"""
from __future__ import annotations

import json
import re
import time
import uuid

from . import runtime

RULES = """MiniMax H3 follows wording literally. Write plainly, in positive terms; let the reference pictures carry the visual detail.
1. Name only what IS in frame. Never list what must not appear - naming a thing summons it.
2. Every shot says where the camera actually is and what is behind the subjects as seen from there (the background in that direction, not the place in general).
3. Directions are screen-relative: "toward the LEFT side of the frame". Say who is on which side. Two-shots: "the same distance from the camera and the same size in the frame".
4. Keep familiar features short and point to the picture ("exactly as in <Picture 1>"); no stroke-by-stroke detail.
5. Expressions are physical (eyes, brows, mouth, head tilt); each beat has its expression and says when it returns.
6. Each event happens once, in one beat only; an action already under way starts "from the very first frame".
7. One spoken line per part, with its exact time window, ending clearly on its last character, then 1.5-3 s of silence.
8. H3 lip-syncs whoever is visible: a line spoken by someone off screen does not belong in the prompt - it is a separate voice-over.
9. A text description does not reliably set a voice's gender or age: every speaker uses a voice reference (<Audio N>).
10. Hands: exactly two hands in frame, each with five fingers; say which fingers touch. Cold air: no one's breath is ever visible.
11. Revisions keep every accepted decision and the character descriptions word for word."""

PLAN_SYSTEM = ("You plan short films for MiniMax H3, one video clip per part.\n" + RULES + "\n"
               "Plan the cast, the places and the parts. style: the look in one line, e.g. \"Frieren-style 2D anime with "
               "clean line art, soft cel shading\" or \"Live-action, cinematic, natural light\". subjects: one per character "
               "who appears anywhere, with a short definition that starts with \"the\" (\"the anime schoolgirl with long "
               "black hair, ...\": identity, hair, face, expression baseline, outfit top to bottom - appearance only, no "
               "action and no place) as seen in their character sheet. A part where someone speaks is at least 4 seconds. locations: one per place, briefly. parts: in story order; in_frame lists only the "
               "characters the camera sees; speaker is a character in frame who says line (or empty); a line said by "
               "someone not in frame goes in voiceover_speaker / voiceover_line instead. camera: where the camera stands "
               "and how high, facing what. background: what is behind the subjects from that camera. Answer in JSON.")
PART_SYSTEM = ("You write one MiniMax H3 clip of a storyboard.\n" + RULES + "\n"
               "Write: summary (one to three sentences: shot, who does what, where, the emotional beat); expressions (each "
               "character's expression arc in one sentence); framing (camera height, crop, who is on which side of the "
               "frame, what is blurred behind); delivery (how the line is spoken, or empty); beats (timed, covering the "
               "whole clip in 2 to 4 short beats, each with action, expression and where the eyes look; the spoken line "
               "exactly once, word for word); constraints (at most two things this particular shot tends to get wrong - "
               "a pose, a prop, hands, the eyes - each a short title and its text; not the camera, the cast or the "
               "dialogue, those are written already); "
               "audio (music or 'no background music', footsteps, ambient sounds). Refer to the characters by their tags "
               "(<Subject 1>, ...). Answer in JSON.")

PLAN_SCHEMA = {"type": "object", "properties": {
    "title": {"type": "string"}, "changes": {"type": "string"}, "style": {"type": "string"},
    "subjects": {"type": "array", "items": {"type": "object", "properties": {
        "name": {"type": "string"}, "definition": {"type": "string"}}, "required": ["name", "definition"]}},
    "locations": {"type": "array", "items": {"type": "object", "properties": {
        "name": {"type": "string"}, "description": {"type": "string"}}, "required": ["name", "description"]}},
    "parts": {"type": "array", "items": {"type": "object", "properties": {
        "label": {"type": "string"}, "seconds": {"type": "number"}, "shot": {"type": "string"},
        "location": {"type": "string"}, "camera": {"type": "string"}, "background": {"type": "string"},
        "in_frame": {"type": "array", "items": {"type": "string"}}, "action": {"type": "string"},
        "speaker": {"type": "string"}, "line": {"type": "string"}, "language": {"type": "string"},
        "voiceover_speaker": {"type": "string"}, "voiceover_line": {"type": "string"}},
        "required": ["label", "seconds", "shot", "location", "camera", "background", "in_frame", "action", "speaker", "line"]}},
    "changed_parts": {"type": "array", "items": {"type": "integer"}}},
    "required": ["title", "style", "subjects", "locations", "parts"]}
PART_SCHEMA = {"type": "object", "properties": {
    "summary": {"type": "string"}, "expressions": {"type": "string"}, "framing": {"type": "string"},
    "delivery": {"type": "string"},
    "beats": {"type": "array", "items": {"type": "object", "properties": {
        "start": {"type": "number"}, "end": {"type": "number"}, "text": {"type": "string"}}, "required": ["start", "end", "text"]}},
    "constraints": {"type": "array", "items": {"type": "object", "properties": {
        "title": {"type": "string"}, "text": {"type": "string"}}, "required": ["title", "text"]}},
    "audio": {"type": "string"}},
    "required": ["summary", "expressions", "framing", "beats", "audio"]}

MAX_PARTS = 8
SHOT_SIZES = ["Extreme wide", "Wide", "Full", "Medium", "Medium close-up", "Close-up", "Extreme close-up",
              "Over the shoulder", "Two-shot", "Insert", "POV", "Aerial"]
CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")


def _clean(v, n: int = 600) -> str:
    return " ".join(str(v or "").split())[:n].strip()


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).strip()


# ------------------------------------------------------------------------------------------------ the plan in code
def normalise(raw: dict, request: str) -> dict:
    """The planner's JSON made safe: slots assigned in a fixed order, speakers checked against who is in frame
    (an off-screen line becomes a voice-over), lengths clamped."""
    subjects = []
    for s in (raw.get("subjects") or [])[:6]:
        name = _clean(s.get("name"), 60)
        if name and _norm(name) not in [_norm(x["name"]) for x in subjects]:
            d = _clean(s.get("definition"), 500).rstrip(".")
            if d and not re.match(r"(?i)(the|a|an)\b", d):
                d = "the character with " + d[0].lower() + d[1:]
            subjects.append({"name": name, "definition": d or "the character"})
    locations = []
    for loc in (raw.get("locations") or [])[:4]:
        name = _clean(loc.get("name"), 60)
        if name and _norm(name) not in [_norm(x["name"]) for x in locations]:
            locations.append({"name": name, "description": _clean(loc.get("description"), 300).rstrip(".")})
    if not locations:
        locations = [{"name": "the location", "description": "the setting of the scene"}]
    # fixed slots: characters first (Picture i, Audio i), then the places
    for i, s in enumerate(subjects):
        s["tag"], s["picture"], s["audio"] = f"<Subject {i + 1}>", i + 1, i + 1
    for j, loc in enumerate(locations):
        loc["picture"] = len(subjects) + j + 1
    by_name = {_norm(s["name"]): s for s in subjects}

    def find(name: str) -> dict | None:
        n = _norm(name)
        if not n:
            return None
        if n in by_name:
            return by_name[n]
        return next((s for k, s in by_name.items() if n in k or k in n), None)

    parts = []
    for p in (raw.get("parts") or [])[:MAX_PARTS]:
        in_frame = []
        for nm in p.get("in_frame") or []:
            s = find(nm)
            if s and s["name"] not in in_frame:
                in_frame.append(s["name"])
        speaker, line = find(p.get("speaker") or ""), _clean(p.get("line"), 300)
        vo_speaker, vo_line = find(p.get("voiceover_speaker") or ""), _clean(p.get("voiceover_line"), 300)
        notes = []
        if line and speaker and speaker["name"] not in in_frame:
            # H3 lip-syncs whoever is visible: an off-screen line goes to a separate voice-over
            vo_speaker, vo_line, speaker, line = speaker, line, None, ""
            notes.append("moved-offscreen")
        if line and not speaker:
            if len(in_frame) == 1:
                speaker = by_name[_norm(in_frame[0])]
            else:
                vo_line, line = line, ""
        loc = next((x for x in locations if _norm(x["name"]) == _norm(p.get("location") or "")), None) or \
            next((x for x in locations if _norm(p.get("location") or "") and _norm(p.get("location") or "") in _norm(x["name"])), None) or locations[0]
        try:
            seconds = float(p.get("seconds") or 5)
        except (TypeError, ValueError):
            seconds = 5.0
        if line:          # lead-in + the line + at least 1.5 s of silence
            seconds = max(seconds, round((1.0 + _line_seconds(line, "") + 2.0) * 2 + 0.49) / 2)
        parts.append({"label": _clean(p.get("label"), 60) or f"Part {len(parts) + 1}", "seconds": max(3.0, min(15.0, round(seconds * 2) / 2)),
                      "shot": _clean(p.get("shot"), 80) or "Medium shot", "location": loc["name"],
                      "camera": _clean(p.get("camera"), 200), "background": _clean(p.get("background"), 240),
                      "in_frame": in_frame, "action": _clean(p.get("action"), 500),
                      "speaker": speaker["name"] if speaker else "", "line": line,
                      "language": _clean(p.get("language"), 30) or ("Japanese" if CJK.search(line) else "English"),
                      "voiceover_speaker": vo_speaker["name"] if vo_speaker else "", "voiceover_line": vo_line, "flags": notes})
    # a line is said once in the whole film: a copy in another part (a small model repeats the answer) is dropped,
    # keeping the part that has no voice-over over it, else the first
    seen: dict[str, int] = {}
    for k, p in enumerate(parts):
        key = _norm(p["line"])
        if not key:
            continue
        if key in seen:
            j = seen[key]
            drop = j if parts[j]["voiceover_line"] and not p["voiceover_line"] else k
            if drop == j:
                seen[key] = k
            parts[drop]["line"], parts[drop]["speaker"] = "", ""
            parts[drop]["flags"].append("duplicate-line")
        else:
            seen[key] = k
    if not parts:
        parts = [{"label": "Part 1", "seconds": 5.0, "shot": "Medium shot", "location": locations[0]["name"], "camera": "",
                  "background": "", "in_frame": [s["name"] for s in subjects[:1]], "action": _clean(request, 400),
                  "speaker": "", "line": "", "language": "English", "voiceover_speaker": "", "voiceover_line": "", "flags": []}]
    return {"title": _clean(raw.get("title"), 80) or "Storyboard", "style": _clean(raw.get("style"), 200).rstrip("."),
            "subjects": subjects, "locations": locations, "parts": parts}


def _lc(text: str) -> str:
    """Lower-case the first letter of a phrase that goes mid-sentence ("is A snowy lakeside" -> "is a snowy lakeside"),
    leaving acronyms and tags alone."""
    return text[:1].lower() + text[1:] if len(text) > 1 and text[0].isupper() and text[1].islower() else text


def _quote(line: str) -> str:
    return f"「{line}」" if CJK.search(line) else f"\"{line}\""


def _last_char(line: str) -> str:
    core = re.sub(r"[\s.,!?。、！？…」」\"']+$", "", line)
    if not core:
        return line[-1:] if line else ""
    if CJK.search(core[-1]):
        return core[-1]
    return core.split()[-1] if core.split() else core


def _line_seconds(line: str, language: str) -> float:
    """How long the line takes: Japanese ~7 mora/s (about one kana or kanji each), others ~2.6 words/s."""
    if CJK.search(line):
        return max(1.0, len(re.sub(r"[\s、。！？「」…]", "", line)) / 6.5)
    return max(1.0, len(line.split()) / 2.6)


def _tagger(spec: dict):
    """Text the model wrote names the characters; the prompt refers to them by their tags (<Subject 1>)."""
    pairs = sorted(((s["name"], s["tag"]) for s in spec["subjects"]), key=lambda x: -len(x[0]))
    pats = [(re.compile(rf"(?<![\w<]){re.escape(n)}(?!\w)", re.I), t) for n, t in pairs if n]

    def tag(text: str) -> str:
        for pat, t in pats:
            text = pat.sub(t, text)
        return text
    return tag


DUPLICATE_CONSTRAINT = re.compile(r"(?i)\b(opening|framing|frame|camera|voice|dialogue|line|speak|spoken|silence|silent|character|cast|"
                                  r"extra|other people|background)\b")


def assemble(spec: dict, i: int, w: dict) -> tuple[str, list[str], list[str]]:
    """(the four-section prompt, notes, what to attach) for part i, from the plan and the part writer's fields."""
    tag = _tagger(spec)
    w = {**w, **{k: tag(str(w.get(k) or "")) for k in ("summary", "expressions", "framing", "delivery", "audio")},
         "beats": [{**b, "text": tag(str(b.get("text") or ""))} for b in (w.get("beats") or []) if isinstance(b, dict)],
         "constraints": [{**c, "text": tag(str(c.get("text") or ""))} for c in (w.get("constraints") or []) if isinstance(c, dict)]}
    p = dict(spec["parts"][i])
    for k in ("shot", "camera", "background", "action"):
        p[k] = tag(p[k])
    subs = {s["name"]: s for s in spec["subjects"]}
    loc = next(x for x in spec["locations"] if x["name"] == p["location"])
    shown = [subs[n] for n in p["in_frame"] if n in subs]
    speaker = subs.get(p["speaker"]) if p["line"] else None
    secs = float(p["seconds"])
    tags = [s["tag"] for s in shown]
    who = " and ".join(tags) if len(tags) <= 2 else ", ".join(tags[:-1]) + " and " + tags[-1]
    notes: list[str] = []

    # subject_definitions - only who is in frame is described; every other slot is declared unused, unnamed
    sd = []
    for s in shown:
        line = f"{s['tag']} is {s['definition']}, exactly as in <Picture {s['picture']}>."
        if speaker and speaker is s:
            line += f" {s['tag']} speaks with the exact voice in <Audio {s['audio']}>."
        sd.append(line)
    unused = []
    for s in spec["subjects"]:
        if s in shown:
            if not (speaker and speaker is s):
                unused.append(f"<Audio {s['audio']}>")
            continue
        unused += [f"<Picture {s['picture']}>", f"<Audio {s['audio']}>"]
    for x in spec["locations"]:
        if x is not loc:
            unused.append(f"<Picture {x['picture']}>")
    if unused:
        sd.append(f"{', '.join(unused[:-1]) + ' and ' + unused[-1] if len(unused) > 1 else unused[0]} "
                  f"{'are' if len(unused) > 1 else 'is'} not used in this clip.")
    cam = _lc(p["camera"] or "at eye level, facing the subjects")
    bg = _lc(p["background"] or loc["description"])
    sd.append(f"<Picture {loc['picture']}> is {_lc(loc['description'])}. In this shot the camera is {cam.rstrip('.')}; "
              f"behind {who or 'the scene'} is {bg.rstrip('.')}.")

    summary = _clean(w.get("summary"), 700) or p["action"]
    ret = []
    for s in shown:
        spoke = speaker is s
        ret.append(f"{s['tag']} (appears in [Shot 1]): fully_preserved - face, hair and outfit are kept exactly as in "
                   f"<Picture {s['picture']}>" + (f"; the voice is kept exactly as in <Audio {s['audio']}>" if spoke else "")
                   + "; " + ("speaks the one line below." if spoke else "does not speak."))
    expr = _clean(w.get("expressions"), 400)
    if expr:
        ret.append(f"Expressions: {expr.rstrip('.')}.")
    ret.append(f"Setting: {loc['name']} is kept exactly as in <Picture {loc['picture']}>.")

    dd = []
    style = spec.get("style") or "Cinematic"
    dd.append(f"{style}. " + ((f"Only {who} appears in this clip, exactly once; no other characters." if len(tags) == 1 else
                               f"Only {who} appear in this clip, each exactly once; no other characters.")
                              if tags else "No characters appear in this clip."))
    shot_name = p["shot"].rstrip(".")
    shot_name = shot_name[:1].lower() + shot_name[1:]
    dd.append(f"Opening (must be followed exactly): This clip is a hard cut. The very first frame is already this exact "
              f"{shot_name}, locked off. The camera does not orbit, pan, swing around, push in or move in any way.")
    framing = _clean(w.get("framing"), 500).rstrip(".")
    framing = framing[:1].upper() + framing[1:]
    dd.append(f"Framing (must be followed exactly): " + ("" if re.match(r"(?i)(the )?camera\b", framing) else f"The camera is {cam.rstrip('.')}. ")
              + (f"{framing}. " if framing else "")
              + f"Behind {who or 'the scene'}, as seen from the camera, is {bg.rstrip('.')}.")
    tail = 0.0
    if speaker:
        delivery = _clean(w.get("delivery"), 200).rstrip(".")
        if not delivery:          # the model often puts the delivery in a beat instead
            said = " ".join([str(w.get("summary") or "")] + [str(b.get("text") or "") for b in w.get("beats") or []]).lower()
            delivery = next((d for k, d in (("whisper", "in a soft whisper"), ("shout", "loud, shouting"), ("soft", "softly, quietly"))
                             if k in said), "soft, natural pace")
        dd.append(f"Voice (must be followed exactly): {speaker['tag']} speaks only in the voice from <Audio {speaker['audio']}>, "
                  f"{delivery}. <Audio {speaker['audio']}> is a voice reference only; its own audio is never played in the video.")
        need = _line_seconds(p["line"], p["language"])
        start = min(1.0, max(0.3, secs * 0.15))
        end = round(min(secs - 1.5, start + need), 1)
        tail = round(secs - end, 1)
        if end - start < need * 0.85:
            notes.append(f"The line needs about {need:.1f} s and leaves less than 1.5 s of silence in {secs:g} s - make the part longer or the line shorter.")
        dd.append(f"Dialogue (must be followed exactly): Only {speaker['tag']} speaks. The one line below is said only once, complete "
                  f"from first to last syllable and ending clearly on {_quote(_last_char(p['line']))}. Nothing is cut off and nothing "
                  f"is repeated. After it, there are about {max(1.5, tail):.1f} seconds of silence before the video ends.")
    for s in shown:
        if s is not speaker:
            dd.append(f"Silence (must be followed exactly): {s['tag']} does not speak, whisper, hum, sigh or make any vocal sound; "
                      "their lips stay closed.")
    for c in (w.get("constraints") or [])[:3]:
        title, text = _clean(c.get("title"), 40).rstrip(":."), _clean(c.get("text"), 400)
        if title and text and not DUPLICATE_CONSTRAINT.search(title):
            dd.append(f"{title[0].upper() + title[1:]} (must be followed exactly): {text.rstrip('.')}.")
    beats = []
    for b in w.get("beats") or []:
        try:
            a, z = max(0.0, float(b.get("start"))), min(secs, float(b.get("end")))
        except (TypeError, ValueError):
            continue
        t = _clean(b.get("text"), 600)
        if speaker and p["line"]:
            # the line is written once, by the code, in the skill's lip-sync wording: take it out of the model's beats
            t0 = t
            t = re.sub(r"[\"'「“‘]?\s*" + re.escape(p["line"].strip(" .!?。！？")) + r"[\s.!?。！？]*[\"'」”’]?", " ", t, flags=re.I)
            t = re.sub(r"(?i)\b(says|said|saying|replies|answers|whispers)\b[^.]*?:\s*$", "", t.strip()).strip(" ,:;")
            t = " ".join(t.split())
            if t != " ".join(t0.split()) and len(re.findall(r"\w+", t)) < 3:
                continue                       # the beat was only the line
        if t and z > a:
            beats.append((a, z, t))
    beats.sort()
    if speaker:
        # the line in exactly one beat, at its time window, in the skill's words
        start = min(1.0, max(0.3, secs * 0.15))
        end = round(min(secs - 1.5, start + _line_seconds(p["line"], p["language"])), 1)
        kept = []
        for a, z, t in beats:                  # a beat across the line's window keeps its part before it; after it is silence
            if z <= start or a >= end:
                kept.append((a, z, t))
                continue
            if a < start:
                kept.append((a, start, t))
            if z > end and a >= start:
                kept.append((end, z, "Silence. The pose is held."))
        beats = kept
        beats.append((start, end, f"{speaker['tag']} says in {p['language']}, in the voice from <Audio {speaker['audio']}>: "
                                   f"{_quote(p['line'])} The lips move in sync with every syllable through to the final "
                                   f"{_quote(_last_char(p['line']))}, then close."))
        beats.sort()
    if not beats:
        beats = [(0.0, secs, p["action"] or summary)]
    if speaker and beats[-1][1] < secs - 0.2:
        beats.append((beats[-1][1], secs, "Silence. Hold."))
    shot = [f"[Shot 1] [0.0s–{secs:g}s] {p['shot'].rstrip('.')}."] + [f"- [{a:.1f}s–{z:.1f}s] {t}" for a, z, t in beats]
    audio = _clean(w.get("audio"), 500).rstrip(".") or "No background music"
    if speaker:
        end_c = _quote(_last_char(p["line"]))
        audio += f". {speaker['tag']}'s one line, ending on {end_c}, then silence"
        audio += ". The reference audio itself is not played"
    else:
        audio += ". No voices at all"
    prompt = ("subject_definitions:\n" + "\n".join(sd) + "\n\nsummary:\n[reference generation] " + summary.rstrip(".") + ".\n\n"
              "retention_analysis:\n" + "\n".join(ret) + "\n\ndetailed_description:\n" + "\n\n".join(dd) + "\n\n"
              + "\n".join(shot) + f"\n\nAudio: {audio}.")
    attach = [f"Picture {s['picture']}" for s in shown] + [f"Picture {loc['picture']}"] + ([f"Audio {speaker['audio']}"] if speaker else [])
    notes.insert(0, "Attach " + ", ".join(attach) + (" - no audio: an attached voice makes characters talk." if not speaker else "."))
    if p["voiceover_line"]:
        vs = subs.get(p["voiceover_speaker"])
        vo = f"Voice-over{' for ' + vs['name'] if vs else ''}: {_quote(p['voiceover_line'])} - record it in Voice Studio" + \
             (f" with {vs['name']}'s voice reference" if vs else "") + " and lay it over this clip in the editor. H3 lip-syncs whoever is visible, so it is not in the prompt."
        notes.append(vo)
    if speaker:
        notes.append(f"Use a voice reference for {speaker['name']} as Audio {speaker['audio']} - a text description does not reliably set gender or age.")
    return prompt, notes, attach


def references(spec: dict) -> list[dict]:
    out = [{"slot": f"Picture {s['picture']}", "content": f"{s['name']} - character sheet"} for s in spec["subjects"]]
    out += [{"slot": f"Picture {x['picture']}", "content": f"{x['name']} - location view"} for x in spec["locations"]]
    out += [{"slot": f"Audio {s['audio']}", "content": f"{s['name']} - voice reference (only in parts where they speak on screen)"} for s in spec["subjects"]]
    return out


# ------------------------------------------------------------------------------------------------ the calls
def _part_brief(spec: dict, i: int) -> str:
    p = spec["parts"][i]
    subs = {s["name"]: s for s in spec["subjects"]}
    cast = "; ".join(f"{subs[n]['tag']} = {n}: {subs[n]['definition']}" for n in p["in_frame"] if n in subs) or "no characters"
    loc = next(x for x in spec["locations"] if x["name"] == p["location"])
    said = (f"{subs[p['speaker']]['tag']} says in {p['language']}: {_quote(p['line'])}" if p["line"] and p["speaker"] in subs
            else "Nobody in frame speaks.")
    return (f"Look: {spec.get('style') or 'cinematic'}.\nPart {i + 1} of {len(spec['parts'])}: {p['label']}, {p['seconds']:g} seconds, {p['shot']}.\n"
            f"In frame (only these): {cast}.\nPlace: {loc['name']} - {loc['description']}.\nCamera: {p['camera'] or 'eye level'}. "
            f"Behind them from there: {p['background'] or loc['description']}.\nWhat happens: {p['action']}\n{said}")


async def plan(request: str, history: list[dict], prev: dict | None, device: str, edited: dict | None = None) -> tuple[dict, dict]:
    t0 = time.time()
    calls = []
    if prev and edited and isinstance(edited.get("parts"), list):
        for part, txt in zip(prev.get("parts") or [], edited["parts"]):
            if isinstance(txt, str) and txt.strip():
                part["prompt"] = txt                                   # the panel's edits are kept
    prev_spec = (prev or {}).get("spec")
    convo = "\n".join(f"{m['role']}: {m['text']}" for m in history[-4:])
    if prev_spec:
        brief = {k: prev_spec[k] for k in ("title", "style", "subjects", "locations")}
        brief["parts"] = [{k: v for k, v in p.items() if k != "flags"} for p in prev_spec["parts"]]
        user = (f"The current plan:\n{json.dumps(brief, ensure_ascii=False)}\n\nChange request: {request}\n\n"
                "Return the whole plan with the change applied (keep everything else as it is, the subject definitions word for "
                "word), changes = one or two sentences on what changed and why, changed_parts = the numbers (1-based) of the "
                "parts whose clip changes. Answer in JSON.")
    else:
        user = f"Conversation:\n{convo}\n\nRequest: {request}\n\nPlan it (at most {MAX_PARTS} parts). Answer in JSON."
    out, info = await runtime.chat([{"role": "system", "content": PLAN_SYSTEM}, {"role": "user", "content": user}],
                                   PLAN_SCHEMA, device=device, keep=True, temperature=0.4)
    calls.append(info)
    raw = runtime.parse_json(out)
    spec = normalise(raw, request)
    old_parts = (prev or {}).get("parts") or []
    global_change = bool(prev_spec) and any(json.dumps(spec[k], sort_keys=True) != json.dumps(prev_spec.get(k), sort_keys=True)
                                            for k in ("style", "subjects", "locations"))
    changed = {int(n) - 1 for n in (raw.get("changed_parts") or []) if isinstance(n, (int, float))}
    parts, writes = [], {}
    for i, p in enumerate(spec["parts"]):
        before = prev_spec["parts"][i] if prev_spec and i < len(prev_spec["parts"]) else None
        same = before is not None and json.dumps({k: v for k, v in p.items() if k != "flags"}, sort_keys=True) == \
            json.dumps({k: v for k, v in before.items() if k != "flags"}, sort_keys=True)
        if prev_spec and same and not global_change and i not in changed and i < len(old_parts):
            parts.append({**old_parts[i], "label": p["label"], "seconds": p["seconds"], "rewritten": False})
            continue
        msgs = [{"role": "system", "content": PART_SYSTEM}, {"role": "user", "content": _part_brief(spec, i) + "\n\nWrite this clip. Answer in JSON."}]
        out2, info2 = await runtime.chat(msgs, PART_SCHEMA, device=device, keep=i < len(spec["parts"]) - 1, temperature=0.4)
        calls.append(info2)
        w = runtime.parse_json(out2)
        writes[i] = out2
        prompt, notes, attach = assemble(spec, i, w)
        if "moved-offscreen" in p["flags"]:
            notes.insert(1, "The line is said by someone who is not in this shot, so it was moved to a voice-over.")
        parts.append({"label": p["label"], "seconds": p["seconds"], "shot": p["shot"], "prompt": prompt, "notes": notes,
                      "attach": attach, "rewritten": True})
    await runtime.unload()
    runtime.debug_dump("vprompt", {"plan": out, "parts": writes})
    redo = [i + 1 for i, p in enumerate(parts) if p.get("rewritten")]
    changes = _clean(raw.get("changes"), 400) if prev_spec else ""
    summary = (f"{changes} " if changes else "") + (
        f"Rewrote part{'s' if len(redo) > 1 else ''} {', '.join(map(str, redo))} - regenerate {'those' if len(redo) > 1 else 'that'} clip{'s' if len(redo) > 1 else ''}."
        if prev_spec else f"{len(parts)} part{'s' if len(parts) > 1 else ''} for MiniMax H3, {sum(p['seconds'] for p in parts):g} s in all.")
    p_out = {"studio": "vprompt", "title": spec["title"], "summary": summary.strip(), "changes": changes,
             "references": references(spec), "parts": parts, "spec": spec, "notes": [], "regenerate": redo if prev_spec else []}
    return p_out, {"seconds": round(time.time() - t0, 1), "device": device, "calls": calls}


async def send_to_video(port: int, p: dict, edited: list[str] | None = None, aspect: str = "16:9") -> dict:
    """The parts as storyboard shots of a new Video Studio project (its editor's Storyboard tab), through its entrance.
    Nothing renders: each shot is a card with its prompt and length, ready for Animate / Generate there."""
    import httpx

    w, h = {"16:9": (1280, 720), "9:16": (720, 1280), "1:1": (960, 960)}.get(aspect, (1280, 720))
    shots = []
    for i, part in enumerate(p.get("parts") or []):
        text = (edited[i] if edited and i < len(edited) and isinstance(edited[i], str) and edited[i].strip() else part["prompt"])
        size = next((z for z in SHOT_SIZES if z.lower() in str(part.get("shot") or "").lower()), "")
        shots.append({"id": "s" + uuid.uuid4().hex[:10], "prompt": text, "seconds": float(part["seconds"]), "model": "h3",
                      "takes": [], "chosen": None, "shot_size": size,
                      "notes": f"{part['label']} · " + " ".join(part.get("notes") or [])[:600], "refs": None})
    body = {"name": f"Assistant · {p.get('title') or 'Storyboard'}"[:120], "width": w, "height": h, "fps": 24.0,
            "clips": [], "audio": [], "texts": [], "shots": shots,
            "board": {"model": "h3", "size_ltx25": "540p", "size_h3": "fast", "cast": []}}
    async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10)) as c:
        r = await runtime.studio_post(c, f"http://127.0.0.1:{port}/api/projects", json=body)
    if r.status_code != 200:
        raise RuntimeError(f"Video Studio: {r.text[:300]}")
    d = r.json()
    return {"project": d.get("id"), "name": d.get("name"), "shots": len(shots)}
