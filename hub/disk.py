"""Storage: how much space each studio uses, by category, and Video Studio's clean-up.

Usage is measured on disk (os.scandir, symlinks not followed) and cached for a minute. For Video Studio the files are
also sorted by what they belong to, from its job list - its own API when it runs, and always its jobs database read
read-only (the API only holds the newest 500 jobs in memory, so a job missing there may still exist):

    History videos              the results in History (output, thumbnail, extra files)       - shown only
    Storyboard shot clips       data/inputs/<job>-segNN.mp4 of jobs in History                - cleanable, opt-in
    "Before refine" copies      outputs/<job>-unrefined.mp4 of jobs in History                - cleanable, opt-in
    Trash older than 7 days     jobs in Video Studio's Trash                                  - cleanable, default
    Trash (last 7 days)                                                                       - cleanable, opt-in
    Orphans                     outputs / thumbnails / shot clips whose job no longer exists   - cleanable, default
    Uploads, engine scratch, temp                                                              - shown only

Nothing is ever deleted except by cleanup(), which the shell calls only after the user confirmed the exact list
(a dry run first). cleanup() recomputes the plan and acts only on items that are still in it, in a category the
user ticked. A Trash item is deleted through Video Studio's own API (DELETE /api/jobs/<id> purges a trashed job),
so its database and History stay consistent; files without an owner (orphans, the shot clips and "before refine"
copies the user chose) are removed by the hub, and only inside outputs/ and data/inputs/. A file newer than an hour,
or whose name appears in any job, project or character record, is never an orphan.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .core import Hub
    from .process import ManagedProcess

JOB_ID = r"\d{8}-\d{6}-[0-9a-f]{6}"
OUTPUT_FILE = re.compile(rf"^({JOB_ID})(?:-unrefined|-trimmed|-rgba|-matte|-final)?\.(?:mp4|webm|mov|png|jpg)$")
THUMB_FILE = re.compile(rf"^({JOB_ID})\.(?:jpg|jpeg|png|webp)$")
SHOT_FILE = re.compile(rf"^({JOB_ID})-seg\d+\.mp4$")
TRASH_DAYS = 7
MIN_AGE_S = 3600           # a file this new may belong to a job that is being written right now
CACHE_S = 60.0


def tree_size(root: Path, skip: tuple[Path, ...] = ()) -> tuple[int, int]:
    """(bytes, files) under root, symlinks not followed; folders in `skip` are left out."""
    total = files = 0
    skip_s = {str(p) for p in skip}
    stack = [root]
    while stack:
        d = stack.pop()
        if str(d) in skip_s:
            continue
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                            files += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return total, files


def _fsize(p: Path) -> int:
    try:
        return p.stat().st_size if p.is_file() and not p.is_symlink() else 0
    except OSError:
        return 0


def _cat(cid: str, label: str, help_: str, cleanable: bool, default: bool = False) -> dict[str, Any]:
    return {"id": cid, "label": label, "help": help_, "cleanable": cleanable, "default": default,
            "bytes": 0, "files": 0, "items": []}


# ====================================================================== Video Studio's job list
def read_jobs_db(db: Path) -> list[dict[str, Any]] | None:
    """Every job in Video Studio's database, opened read-only (None when it cannot be read)."""
    if not db.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute("SELECT data FROM jobs").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    out = []
    for (data,) in rows:
        try:
            out.append(json.loads(data))
        except (TypeError, ValueError):
            continue
    return out


def video_plan(root: Path, api_jobs: list[dict[str, Any]] | None, now: float | None = None) -> dict[str, Any]:
    """Video Studio's files sorted into categories (see the module doc). `api_jobs`: History + Trash from its API, or
    None when it is not running. Pure filesystem + data: no network, nothing deleted."""
    now = now or time.time()
    outputs, inputs = root / "outputs", root / "data" / "inputs"
    db_jobs = read_jobs_db(root / "data" / "jobs.sqlite")
    jobs: dict[str, dict[str, Any]] = {}
    for j in db_jobs or []:
        if j.get("id"):
            jobs[j["id"]] = j
    for j in api_jobs or []:                     # the running studio's view wins (Trash state is live there)
        if j.get("id"):
            jobs[j["id"]] = {**jobs.get(j["id"], {}), **j}
    readable = db_jobs is not None or api_jobs is not None
    # Every name any record mentions: a file named there is never an orphan, whatever its name looks like.
    blob_parts = [json.dumps(list(jobs.values()), default=str)]
    for extra in [*sorted((root / "data" / "projects").glob("*.json")), root / "data" / "characters.json"]:
        try:
            blob_parts.append(extra.read_text("utf-8", errors="replace"))
        except OSError:
            pass
    blob = "\n".join(blob_parts)

    cats = {c["id"]: c for c in (
        _cat("history", "History videos", "The results in Video Studio's History. Delete them in Video Studio (they go to its Trash first).", False),
        _cat("history_shots", "Storyboard shot clips (History)", "The kept shot clips of storyboards that are in History. Video Studio uses them to re-roll or re-cut a single shot - the joined video stays.", True),
        _cat("history_unrefined", "\"Before refine\" copies (History)", "The video before Refine faces, kept for the compare view. The refined video stays.", True),
        _cat("trash_old", f"Trash older than {TRASH_DAYS} days", "Jobs in Video Studio's Trash for over a week, with their files. Deleted through Video Studio, so its History stays consistent.", True, True),
        _cat("trash_recent", f"Trash (last {TRASH_DAYS} days)", "Jobs moved to the Trash this week - they can still be restored in Video Studio.", True),
        _cat("orphans", "Orphans", "Videos, thumbnails and shot clips whose job no longer exists anywhere in Video Studio.", True, True),
        _cat("inputs", "Uploads and work files", "Reference pictures, voice samples and source clips in data/inputs (projects may use them).", False),
        _cat("engine", "Engine scratch", "outputs/_engine - the ComfyUI engine's own output folder.", False),
        _cat("temp", "Temporary files", "data/temp", False),
    )}

    counted: set[Path] = set()          # every file some category already counts

    def add(cid: str, item_id: str, label: str, files: list[Path], **extra: Any) -> None:
        files = [f for f in files if _fsize(f) or f.is_file()]
        counted.update(files)
        size = sum(_fsize(f) for f in files)
        c = cats[cid]
        c["bytes"] += size
        c["files"] += len(files)
        if c["cleanable"]:
            c["items"].append({"id": item_id, "label": label, "bytes": size,
                               "files": [str(f.relative_to(root)) for f in files], **extra})

    claimed: set[Path] = set()

    def job_files(j: dict[str, Any]) -> tuple[list[Path], list[Path], list[Path]]:
        """(result files, shot clips, before-refine copy) of a job."""
        main = [outputs / j[k] for k in ("output_file", "thumbnail") if j.get(k)]
        for x in (j.get("params") or {}).get("extra_files") or []:
            if isinstance(x, dict) and x.get("file"):
                main.append(outputs / Path(str(x["file"])).name)
        shots = [inputs / n for n in (j.get("params") or {}).get("shot_files") or []
                 if isinstance(n, str) and n.startswith(f"{j['id']}-seg")]
        shots += [p for p in inputs.glob(f"{j['id']}-seg*.mp4") if p not in shots]
        unref = [outputs / f"{j['id']}-unrefined.mp4"]
        return main, shots, unref

    for jid, j in jobs.items():
        main, shots, unref = job_files(j)
        claimed.update(main + shots + unref)
        title = " ".join(str(j.get("prompt") or j.get("mode") or "").split())[:80]
        if j.get("deleted_at"):
            age = (now - float(j["deleted_at"])) / 86400
            cid = "trash_old" if age >= TRASH_DAYS else "trash_recent"
            add(cid, f"trash:{jid}", title or jid, main + shots + unref, job=jid, age_days=round(age, 1))
        else:
            present = [f for f in main if f.is_file()]
            counted.update(present)
            cats["history"]["bytes"] += sum(_fsize(f) for f in present)
            cats["history"]["files"] += len(present)
            if any(_fsize(f) for f in shots):
                add("history_shots", f"hshot:{jid}", title or jid, shots, job=jid)
            if any(_fsize(f) for f in unref):
                add("history_unrefined", f"hunref:{jid}", title or jid, unref, job=jid)

    # orphans: a job-id-named file whose job is gone, mentioned nowhere, and not brand new
    def orphan_candidates() -> list[Path]:
        out: list[Path] = []
        for folder, pat in ((outputs, OUTPUT_FILE), (outputs / "thumbnails", THUMB_FILE), (inputs, SHOT_FILE)):
            try:
                with os.scandir(folder) as it:
                    for e in it:
                        if e.is_file(follow_symlinks=False) and pat.match(e.name):
                            out.append(Path(e.path))
            except OSError:
                continue
        return out

    if readable and jobs:
        for p in orphan_candidates():
            m = OUTPUT_FILE.match(p.name) or THUMB_FILE.match(p.name) or SHOT_FILE.match(p.name)
            if not m or m.group(1) in jobs or p in claimed or p.name in blob:
                continue
            try:
                if now - p.stat().st_mtime < MIN_AGE_S:
                    continue
            except OSError:
                continue
            add("orphans", f"orphan:{p.relative_to(root)}", p.name, [p])
    else:
        cats["orphans"]["help"] += " (Not checked: Video Studio's job list could not be read.)"
        cats["orphans"]["cleanable"] = False

    # what is left in the folders
    shots_in_cats = {root / f for c in cats.values() for it in c["items"] for f in it["files"]}
    try:
        with os.scandir(inputs) as it:
            for e in it:
                p = Path(e.path)
                if e.is_file(follow_symlinks=False) and p not in shots_in_cats and p not in claimed:
                    cats["inputs"]["bytes"] += _fsize(p)
                    cats["inputs"]["files"] += 1
    except OSError:
        pass
    for cid, d in (("engine", outputs / "_engine"), ("temp", root / "data" / "temp")):
        b, n = tree_size(d)
        cats[cid]["bytes"], cats[cid]["files"] = b, n
    for c in cats.values():
        c["items"].sort(key=lambda it: -it["bytes"])
    in_outputs = sum(_fsize(f) for f in counted if outputs in f.parents) + cats["engine"]["bytes"]
    return {"source": "api+database" if api_jobs is not None else ("database" if db_jobs is not None else "none"),
            "api": api_jobs is not None, "jobs": len(jobs), "categories": list(cats.values()), "outputs_counted": in_outputs}


def _inside(p: Path, bases: tuple[Path, ...]) -> bool:
    try:
        rp = p.resolve()
    except OSError:
        return False
    return any(b.resolve() in rp.parents for b in bases) and not p.is_symlink() and rp.is_file()


class Disk:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self._cache: tuple[float, dict[str, Any]] | None = None

    async def _video_api_jobs(self, t: "ManagedProcess") -> list[dict[str, Any]] | None:
        """History + Trash from the running Video Studio (straight to its backend: listing needs no GPU)."""
        if not t.running:
            return None
        try:
            out: list[dict[str, Any]] = []
            for trash in ("false", "true"):
                r = await self.hub.http.get(f"{t.base_url}/api/jobs?limit=100000&trash={trash}", timeout=15.0)
                if r.status_code != 200:
                    return None
                out += r.json().get("jobs") or []
            return out
        except Exception:
            return None

    # ------------------------------------------------------------------ usage
    async def usage(self, force: bool = False) -> dict[str, Any]:
        if self._cache and not force and time.time() - self._cache[0] < CACHE_S:
            return self._cache[1]
        studios = []
        for tid in self.hub.order:
            t = self.hub.tools[tid]
            api_jobs = await self._video_api_jobs(t) if tid == "video" else None
            studios.append(await asyncio.to_thread(self._studio_usage, t, api_jobs))
        try:
            du = shutil.disk_usage(self.hub.tools[self.hub.order[0]].tool_dir)
            disk = {"total": du.total, "used": du.used, "free": du.free}
        except Exception:
            disk = None
        data = {"studios": studios, "disk": disk, "scanned_at": time.time()}
        self._cache = (time.time(), data)
        return data

    def _models_dirs(self, t: "ManagedProcess") -> list[Path]:
        d = t.tool_dir
        if t.id == "video":
            return [t.spec.models_dir(d)]
        dirs: list[Path] = []
        for k in t.spec.model_kinds:
            top = d / Path(k["dir"]).parts[0]
            if top not in dirs:
                dirs.append(top)
        return dirs

    def _studio_usage(self, t: "ManagedProcess", api_jobs: list[dict[str, Any]] | None) -> dict[str, Any]:
        d = t.tool_dir
        out_dir = t.spec.outputs_dir(d)
        models = [m for m in self._models_dirs(t) if m.is_dir()]
        cats: list[dict[str, Any]] = []
        plan = None
        if t.id == "video":
            plan = video_plan(d, api_jobs)
            for c in plan["categories"]:
                if c["id"] in ("inputs", "temp"):
                    continue
                cats.append({k: c[k] for k in ("id", "label", "bytes", "files", "cleanable", "default", "help")})
            out_b, _n = tree_size(out_dir)
            if out_b > plan["outputs_counted"]:     # anything in outputs/ the categories do not explain
                cats.append({"id": "outputs_other", "label": "Other outputs", "bytes": out_b - plan["outputs_counted"], "files": 0,
                             "cleanable": False, "default": False, "help": "Files in outputs/ that belong to no job pattern"})
            inp = next(c for c in plan["categories"] if c["id"] == "inputs")
            tmp = next(c for c in plan["categories"] if c["id"] == "temp")
            cats.append({k: inp[k] for k in ("id", "label", "bytes", "files", "cleanable", "default", "help")})
            cats.append({k: tmp[k] for k in ("id", "label", "bytes", "files", "cleanable", "default", "help")})
            data_b, data_n = tree_size(d / "data", skip=(d / "data" / "inputs", d / "data" / "temp"))
            cats.append({"id": "data", "label": "Jobs database, projects, settings", "bytes": data_b, "files": data_n,
                         "cleanable": False, "default": False, "help": "data/ without the uploads and temp files"})
        else:
            b, n = tree_size(out_dir)
            cats.append({"id": "outputs", "label": "Outputs", "bytes": b, "files": n, "cleanable": False, "default": False,
                         "help": str(out_dir)})
        shared = t.id == "video" and models and d not in models[0].parents
        for m in models:
            b, n = tree_size(m)
            cats.append({"id": "models", "label": ("Models" if m.name == "models" or len(models) == 1 else f"Models · {m.name}")
                         + (" (shared ComfyUI store)" if shared else ""), "bytes": b,
                         "files": n, "cleanable": False, "default": False,
                         "help": f"{m} - manage them on the Models page"})
        skip = tuple([out_dir, *models] + ([d / "data"] if t.id == "video" else []))
        rest_b, rest_n = tree_size(d, skip=skip)
        cats.append({"id": "other", "label": "Code, environment and caches", "bytes": rest_b, "files": rest_n,
                     "cleanable": False, "default": False, "help": str(d)})
        return {"tool": t.id, "name": t.spec.name, "color": t.spec.color, "dir": str(d),
                "total": sum(c["bytes"] for c in cats if not (c["id"] == "models" and shared)),
                "categories": cats, "cleanup": plan is not None,
                "jobs_source": plan["source"] if plan else None}

    # ------------------------------------------------------------------ clean-up (Video Studio)
    async def plan(self, tool_id: str) -> dict[str, Any]:
        if tool_id != "video" or tool_id not in self.hub.tools:
            raise ValueError("clean-up is available for Video Studio")
        t = self.hub.tools[tool_id]
        api_jobs = await self._video_api_jobs(t)
        p = await asyncio.to_thread(video_plan, t.tool_dir, api_jobs)
        p["running"] = t.running
        return p

    async def cleanup(self, tool_id: str, categories: list[str], item_ids: list[str], dry_run: bool = True) -> dict[str, Any]:
        """Delete exactly the confirmed items that are still in a freshly computed plan, in a ticked category."""
        p = await self.plan(tool_id)
        t = self.hub.tools[tool_id]
        root = t.tool_dir
        bases = (root / "outputs", root / "data" / "inputs")
        wanted = set(item_ids)
        allowed = {c["id"]: c for c in p["categories"] if c["cleanable"] and c["id"] in set(categories)}
        todo = [(cid, it) for cid, c in allowed.items() for it in c["items"] if it["id"] in wanted]
        found = {it["id"] for _, it in todo}
        result: dict[str, Any] = {"dry_run": dry_run, "deleted": [], "skipped": [], "failed": [], "bytes": 0,
                                  "not_found": sorted(wanted - found)}
        if any(cid.startswith("trash") for cid, _ in todo) and not p["api"]:
            result["failed"] += [{"id": it["id"], "label": it["label"], "why": "Video Studio is not running - start it to empty its Trash"}
                                 for cid, it in todo if cid.startswith("trash")]
            todo = [(cid, it) for cid, it in todo if not cid.startswith("trash")]
        for cid, it in todo:
            if dry_run:
                result["deleted"].append({"id": it["id"], "label": it["label"], "bytes": it["bytes"], "files": it["files"], "category": cid})
                result["bytes"] += it["bytes"]
                continue
            freed = 0
            if cid.startswith("trash"):
                jid = it["job"]
                try:
                    r = await self.hub.http.get(f"{t.base_url}/api/jobs/{jid}", timeout=10.0)
                    if r.status_code != 200 or not r.json().get("deleted_at"):
                        result["skipped"].append({"id": it["id"], "label": it["label"], "why": "no longer in the Trash"})
                        continue
                    before = sum(_fsize(root / f) for f in it["files"])
                    r = await self.hub.http.delete(f"{t.base_url}/api/jobs/{jid}", timeout=60.0)   # purges a trashed job
                    if r.status_code >= 400:
                        result["failed"].append({"id": it["id"], "label": it["label"], "why": f"Video Studio: HTTP {r.status_code}"})
                        continue
                except Exception as exc:
                    result["failed"].append({"id": it["id"], "label": it["label"], "why": f"Video Studio: {type(exc).__name__}"})
                    continue
                # the studio's purge leaves the "before refine" copy: it has no owner any more, so it goes too
                for f in it["files"]:
                    fp = root / f
                    if fp.name == f"{jid}-unrefined.mp4" and _inside(fp, bases):
                        try:
                            fp.unlink()
                        except OSError:
                            pass
                freed = before - sum(_fsize(root / f) for f in it["files"])
            else:
                for f in it["files"]:
                    fp = root / f
                    if not _inside(fp, bases):
                        result["failed"].append({"id": it["id"], "label": it["label"], "why": f"{f} is outside outputs/ and data/inputs/"})
                        continue
                    try:
                        size = _fsize(fp)
                        fp.unlink()
                        freed += size
                    except OSError as exc:
                        result["failed"].append({"id": it["id"], "label": it["label"], "why": f"{f}: {exc.strerror}"})
            result["deleted"].append({"id": it["id"], "label": it["label"], "bytes": freed, "files": it["files"], "category": cid})
            result["bytes"] += freed
        if not dry_run and result["deleted"]:
            self._cache = None
            self.hub.bus.log(f"Storage clean-up in {t.spec.name}: {len(result['deleted'])} item(s), "
                             f"{result['bytes'] / 2**30:.2f} GB freed", tool=tool_id, source="storage")
        return result
