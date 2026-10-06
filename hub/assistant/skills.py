"""Skills belong to the studios: each studio repo keeps its own under assistant/skills/*.md (YAML-ish front matter:
name, studio, families, description). The assistant loads only the skills of the studio it is driving - Forge's
assistant knows Forge's model families, Video Studio's (phase 2) only LTX-2.5 and MiniMax H3."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..config import tool_dir


@dataclass
class Skill:
    name: str
    studio: str
    description: str
    body: str
    families: list[str] = field(default_factory=list)
    path: str = ""


def _front(text: str) -> tuple[dict, str]:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            v = v.strip()
            if v.startswith("[") and v.endswith("]"):
                v = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
            meta[k.strip()] = v
    return meta, m.group(2).strip()


def load_skills(tool_id: str) -> list[Skill]:
    folder = tool_dir(tool_id) / "assistant" / "skills"
    out = []
    for f in sorted(folder.glob("*.md")) if folder.is_dir() else []:
        meta, body = _front(f.read_text("utf-8", errors="replace"))
        out.append(Skill(name=str(meta.get("name") or f.stem), studio=str(meta.get("studio") or tool_id),
                         description=str(meta.get("description") or ""), body=body,
                         families=list(meta.get("families") or []), path=str(f)))
    return out


def general(skills: list[Skill]) -> str:
    """The studio-wide skills (no families): always loaded."""
    return "\n\n".join(s.body for s in skills if not s.families)


def for_family(skills: list[Skill], family: str) -> str:
    return "\n\n".join(s.body for s in skills if family in s.families)
