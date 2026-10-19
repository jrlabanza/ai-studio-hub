"""The Sandbox's records and files: one SQLite database plus a folder per project, under data/sandbox/.

    data/sandbox/sandbox.db                 projects, versions, links, events, views
    data/sandbox/projects/<id>/draft/       the editable working copy
    data/sandbox/projects/<id>/v/<N>/       version N, written once at publish and then made read-only

The rules the database itself enforces (triggers), so no code path - present or future - can break them:

* ``events`` is append-only: no UPDATE, no DELETE. Every change to a project is one row (who, from which address, when).
* ``versions`` are immutable: no UPDATE, no DELETE. Each row holds the sha256 of every file and of the whole set.
* ``links`` are never deleted and a retired link never comes back or moves to another project, so a slug, once
  used, can never show something else: it stays reserved and answers "this link was taken down".

``views`` holds a count per link per day - nothing about who looked.
All methods are synchronous (sqlite3 + file copies); the API calls them from FastAPI's thread pool.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Iterator

from . import intake
from .intake import IntakeError

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,46}[a-z0-9]$")
# Never a link name: hub-ish words, the sandbox server's own paths, and words that read like a system page.
RESERVED = {"api", "static", "assets", "admin", "administrator", "hub", "sandbox", "raw", "v", "draft", "drafts",
            "www", "app", "apps", "login", "logout", "signin", "signup", "register", "auth", "oauth", "account",
            "settings", "config", "new", "edit", "delete", "help", "about", "docs", "doc", "status", "health",
            "metrics", "media", "files", "file", "download", "downloads", "upload", "uploads", "zip", "index",
            "home", "root", "system", "null", "undefined", "none", "favicon", "robots", "sitemap", "security",
            "well-known", "cdn", "public", "private", "internal", "test", "localhost", "brand", "ws", "events"}
MAX_TEXT_EDIT = 2 * 1024 * 1024                  # larger files can be uploaded but not edited in the browser

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS projects(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', author TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'document', kind_auto INTEGER NOT NULL DEFAULT 1, allow_storage INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active', preview_token TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS versions(
  project_id TEXT NOT NULL, number INTEGER NOT NULL, hash TEXT NOT NULL, size INTEGER NOT NULL, files INTEGER NOT NULL,
  manifest TEXT NOT NULL, kind TEXT NOT NULL, entry TEXT, allow_storage INTEGER NOT NULL, author TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '', created REAL NOT NULL, PRIMARY KEY(project_id, number));
CREATE TABLE IF NOT EXISTS links(
  slug TEXT PRIMARY KEY, project_id TEXT NOT NULL, pin INTEGER, status TEXT NOT NULL DEFAULT 'live',
  created REAL NOT NULL, created_by TEXT NOT NULL, retired REAL, retired_by TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, project_id TEXT, project_name TEXT, slug TEXT,
  action TEXT NOT NULL, author TEXT NOT NULL DEFAULT '', ip TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS views(slug TEXT NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(slug, day));
CREATE INDEX IF NOT EXISTS events_project ON events(project_id, id);
CREATE INDEX IF NOT EXISTS links_project ON links(project_id);
CREATE TRIGGER IF NOT EXISTS events_append_only_u BEFORE UPDATE ON events
  BEGIN SELECT RAISE(ABORT, 'the sandbox event log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_append_only_d BEFORE DELETE ON events
  BEGIN SELECT RAISE(ABORT, 'the sandbox event log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS versions_immutable_u BEFORE UPDATE ON versions
  BEGIN SELECT RAISE(ABORT, 'published versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS versions_immutable_d BEFORE DELETE ON versions
  BEGIN SELECT RAISE(ABORT, 'published versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS links_reserved_d BEFORE DELETE ON links
  BEGIN SELECT RAISE(ABORT, 'a link name stays reserved forever'); END;
CREATE TRIGGER IF NOT EXISTS links_fixed_owner BEFORE UPDATE OF slug, project_id, created, created_by ON links
  BEGIN SELECT RAISE(ABORT, 'a link never moves to another project'); END;
CREATE TRIGGER IF NOT EXISTS links_retired_final BEFORE UPDATE ON links WHEN OLD.status = 'retired'
  BEGIN SELECT RAISE(ABORT, 'a retired link is never reused'); END;
"""


class SandboxError(ValueError):
    """A request the Sandbox refuses (the message is shown to the user); ``status`` is the HTTP code."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def check_slug(raw: str) -> tuple[str, str]:
    """(normalised slug, problem or '') for a proposed link name - the format rules only, not availability."""
    slug = str(raw or "").strip().lower()
    if not slug:
        return slug, "Enter a link name"
    if len(slug) < 2 or len(slug) > 48:
        return slug, "2 to 48 characters"
    if not SLUG.match(slug):
        return slug, "Only a-z, 0-9 and hyphens, starting and ending with a letter or digit"
    if slug in RESERVED:
        return slug, f"'{slug}' is reserved"
    return slug, ""


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def overall_hash(manifest: list[dict[str, Any]]) -> str:
    """One sha256 for a set of files: over the sorted "path NUL sha256 LF" lines."""
    h = hashlib.sha256()
    for f in sorted(manifest, key=lambda x: x["path"]):
        h.update(f"{f['path']}\0{f['sha256']}\n".encode("utf-8"))
    return h.hexdigest()


def _clean_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.projects_dir = root / "projects"
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(root / "sandbox.db"), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)

    # ------------------------------------------------------------------ low level
    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _x(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, args)

    def event(self, action: str, author: str, ip: str, project: dict | None = None, slug: str | None = None,
              **detail: Any) -> None:
        self._x("INSERT INTO events(ts, project_id, project_name, slug, action, author, ip, detail) VALUES(?,?,?,?,?,?,?,?)",
                (time.time(), project["id"] if project else None, project["name"] if project else None, slug, action,
                 _clean_text(author, 60) or "unknown", ip or "", json.dumps(detail, ensure_ascii=False)))

    def project_dir(self, pid: str) -> Path:
        return self.projects_dir / pid

    def draft_dir(self, pid: str) -> Path:
        return self.projects_dir / pid / "draft"

    def version_dir(self, pid: str, number: int) -> Path:
        return self.projects_dir / pid / "v" / str(int(number))

    # ------------------------------------------------------------------ projects
    def project(self, pid: str, *, allow_deleted: bool = False) -> dict[str, Any]:
        rows = self._q("SELECT * FROM projects WHERE id=?", (str(pid),))
        if not rows or (rows[0]["status"] == "deleted" and not allow_deleted):
            raise SandboxError("No such project", 404)
        return dict(rows[0])

    def _writable(self, pid: str) -> dict[str, Any]:
        p = self.project(pid)
        if p["status"] == "archived":
            raise SandboxError("This project is archived - restore it to change it", 409)
        return p

    def _touch(self, pid: str, **fields: Any) -> None:
        fields["updated"] = time.time()
        sets = ", ".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE projects SET {sets} WHERE id=?", (*fields.values(), pid))

    def create(self, name: str, description: str, author: str, files: dict[str, bytes], ip: str,
               max_bytes: int) -> dict[str, Any]:
        name = _clean_text(name, 80)
        author = _clean_text(author, 60)
        if not name:
            raise SandboxError("Give the project a name")
        if not author:
            raise SandboxError("Enter your name - it is recorded with every change")
        if sum(len(d) for d in files.values()) > max_bytes:
            raise SandboxError(f"Larger than {max_bytes // 1048576} MB", 413)
        with self._lock:
            pid = secrets.token_hex(4)
            while self._q("SELECT 1 FROM projects WHERE id=?", (pid,)):
                pid = secrets.token_hex(4)
            now = time.time()
            kind = intake.detect_kind(list(files))
            self._x("INSERT INTO projects(id, name, description, author, kind, kind_auto, allow_storage, status, "
                    "preview_token, created, updated) VALUES(?,?,?,?,?,1,0,'active',?,?,?)",
                    (pid, name, str(description or "").strip()[:2000], author, kind, secrets.token_urlsafe(18), now, now))
            self._write_files(pid, files)
            p = self.project(pid)
            self.event("created", author, ip, p, kind=kind)
            if files:
                self.event("uploaded", author, ip, p, files=len(files), bytes=sum(len(d) for d in files.values()),
                           paths=sorted(files)[:50])
        return p

    def update(self, pid: str, patch: dict[str, Any], author: str, ip: str) -> dict[str, Any]:
        p = self._writable(pid)
        changes: dict[str, Any] = {}
        if "name" in patch:
            name = _clean_text(patch["name"], 80)
            if not name:
                raise SandboxError("The name cannot be empty")
            if name != p["name"]:
                changes["name"] = name
        if "description" in patch:
            d = str(patch["description"] or "").strip()[:2000]
            if d != p["description"]:
                changes["description"] = d
        if "kind" in patch:
            kind = str(patch["kind"] or "")
            if kind == "auto":
                changes.update(kind_auto=1, kind=intake.detect_kind([f["path"] for f in self.files(pid)]))
            elif kind in intake.KINDS:
                changes.update(kind=kind, kind_auto=0)
            else:
                raise SandboxError("kind must be web, script, document or auto")
        if "allow_storage" in patch:
            v = 1 if patch["allow_storage"] else 0
            if v != p["allow_storage"]:
                changes["allow_storage"] = v
        if not changes:
            return p
        self._touch(pid, **changes)
        new = self.project(pid)
        for k in changes:
            if k == "kind_auto":
                continue
            action = {"allow_storage": "storage " + ("allowed" if changes[k] else "blocked"),
                      "kind": "kind changed"}.get(k, "details edited")
            self.event(action, author, ip, new, field=k, before=p.get(k) if k != "description" else None,
                       after=changes[k] if k != "description" else None)
        return new

    def list_projects(self, q: str = "", kind: str = "", status: str = "active") -> list[dict[str, Any]]:
        rows = [dict(r) for r in self._q("SELECT * FROM projects WHERE status=? ORDER BY updated DESC", (status,))]
        if kind in intake.KINDS:
            rows = [r for r in rows if r["kind"] == kind]
        if q:
            ql = q.lower()
            links = {}
            for r in self._q("SELECT slug, project_id FROM links"):
                links.setdefault(r["project_id"], []).append(r["slug"])
            rows = [r for r in rows if ql in r["name"].lower() or ql in r["author"].lower()
                    or ql in r["description"].lower() or any(ql in s for s in links.get(r["id"], []))]
        return [self.card(r) for r in rows]

    def card(self, p: dict[str, Any]) -> dict[str, Any]:
        latest = self.latest(p["id"])
        links = self.links(p["id"])
        views = sum(l["views"] for l in links)
        return {**{k: p[k] for k in ("id", "name", "description", "author", "kind", "kind_auto", "allow_storage",
                                     "status", "created", "updated")},
                "latest": latest["number"] if latest else None, "links": [l for l in links if l["status"] == "live"],
                "views": views}

    def set_status(self, pid: str, status: str, author: str, ip: str) -> dict[str, Any]:
        p = self.project(pid)
        if status not in ("active", "archived"):
            raise SandboxError("status must be active or archived")
        if p["status"] != status:
            self._touch(pid, status=status)
            self.event("archived" if status == "archived" else "restored", author, ip, p)
        return self.project(pid)

    def delete(self, pid: str, author: str, ip: str) -> None:
        """Files go; the project row stays as a tombstone (status deleted), every link is retired and stays
        reserved, versions keep their records (hashes, notes) and the event log keeps everything."""
        p = self.project(pid)
        with self._lock:
            for l in self._q("SELECT slug FROM links WHERE project_id=? AND status='live'", (pid,)):
                self._x("UPDATE links SET status='retired', retired=?, retired_by=? WHERE slug=?",
                        (time.time(), _clean_text(author, 60) or "unknown", l["slug"]))
                self.event("link retired", author, ip, p, slug=l["slug"], why="project deleted")
            self._touch(pid, status="deleted", preview_token=secrets.token_urlsafe(18))
            self.event("deleted", author, ip, p, files=len(self.files(pid)))
        d = self.project_dir(pid)
        if d.exists():
            for dirpath, dirnames, filenames in os.walk(d):
                for n in dirnames + filenames:
                    try:
                        os.chmod(os.path.join(dirpath, n), 0o700 if n in dirnames else 0o600, follow_symlinks=False)
                    except OSError:
                        pass
            shutil.rmtree(d, ignore_errors=True)

    def rotate_token(self, pid: str, author: str, ip: str) -> str:
        p = self.project(pid)
        tok = secrets.token_urlsafe(18)
        self._touch(pid, preview_token=tok)
        self.event("preview link renewed", author, ip, p)
        return tok

    # ------------------------------------------------------------------ draft files
    def _draft_path(self, pid: str, rel: str) -> Path:
        clean = intake.clean_path(rel)
        if clean is None:
            raise SandboxError("That file name is not allowed")
        base = self.draft_dir(pid)
        p = base / clean
        # every component inside the draft folder and none of them a symlink
        cur = base
        for part in clean.split("/"):
            cur = cur / part
            if cur.is_symlink():
                raise SandboxError("Links are not allowed", 400)
        if base.resolve() not in p.resolve().parents:
            raise SandboxError("Bad path")
        return p

    def _write_files(self, pid: str, files: dict[str, bytes]) -> None:
        base = self.draft_dir(pid)
        base.mkdir(parents=True, exist_ok=True)
        for rel, data in files.items():
            p = self._draft_path(pid, rel)
            if p.is_dir():
                raise SandboxError(f"{rel} is a folder in the project")
            for parent in p.relative_to(base).parents:
                q = base / parent
                if q.exists() and not q.is_dir():
                    raise SandboxError(f"{parent} is a file in the project, not a folder")
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + ".part~")
            tmp.write_bytes(data)
            os.replace(tmp, p)

    def files(self, pid: str) -> list[dict[str, Any]]:
        return list(self._walk(self.draft_dir(pid)))

    @staticmethod
    def _walk(base: Path) -> Iterator[dict[str, Any]]:
        if not base.is_dir():
            return
        out = []
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not os.path.islink(os.path.join(dirpath, d)))
            for n in filenames:
                full = Path(dirpath) / n
                if full.is_symlink() or n.endswith(".part~"):
                    continue
                st = full.stat()
                out.append({"path": full.relative_to(base).as_posix(), "size": st.st_size, "mtime": st.st_mtime})
        yield from sorted(out, key=lambda f: (f["path"].count("/") > 0, f["path"].lower()))

    def draft_stats(self, pid: str) -> tuple[int, int]:
        fs = self.files(pid)
        return len(fs), sum(f["size"] for f in fs)

    def _check_limits(self, pid: str, extra: dict[str, bytes], max_bytes: int, max_files: int) -> None:
        current = {f["path"]: f["size"] for f in self.files(pid)}
        for rel, data in extra.items():
            current[rel] = len(data)
        if len(current) > max_files:
            raise SandboxError(f"A project holds at most {max_files} files", 413)
        if sum(current.values()) > max_bytes:
            raise SandboxError(f"The project would be larger than {max_bytes // 1048576} MB", 413)

    def _after_files_change(self, pid: str) -> dict[str, Any]:
        p = self.project(pid)
        fields: dict[str, Any] = {}
        if p["kind_auto"]:
            kind = intake.detect_kind([f["path"] for f in self.files(pid)])
            if kind != p["kind"]:
                fields["kind"] = kind
        self._touch(pid, **fields)
        return self.project(pid)

    def add_files(self, pid: str, files: dict[str, bytes], author: str, ip: str, max_bytes: int,
                  max_files: int) -> dict[str, Any]:
        p = self._writable(pid)
        existing = {f["path"] for f in self.files(pid)}
        self._check_limits(pid, files, max_bytes, max_files)
        self._write_files(pid, files)
        replaced = sorted(set(files) & existing)
        self.event("uploaded", author, ip, p, files=len(files), bytes=sum(len(d) for d in files.values()),
                   paths=sorted(files)[:50], replaced=replaced[:50])
        return {"project": self._after_files_change(pid), "added": sorted(set(files) - existing), "replaced": replaced}

    def read_file(self, pid: str, rel: str) -> tuple[Path, bytes]:
        self.project(pid)
        p = self._draft_path(pid, rel)
        if not p.is_file():
            raise SandboxError("No such file", 404)
        return p, p.read_bytes()

    def save_text(self, pid: str, rel: str, text: str, author: str, ip: str, create: bool, max_bytes: int,
                  max_files: int) -> dict[str, Any]:
        p = self._writable(pid)
        path = self._draft_path(pid, rel)
        rel = path.relative_to(self.draft_dir(pid)).as_posix()
        data = str(text).encode("utf-8")
        if len(data) > MAX_TEXT_EDIT:
            raise SandboxError("Too large to save from the editor - upload the file instead", 413)
        exists = path.is_file()
        if create and exists:
            raise SandboxError(f"{rel} already exists", 409)
        if not create and not exists:
            raise SandboxError(f"{rel} no longer exists", 404)
        if exists and path.read_bytes() == data:
            return {"project": p, "path": rel, "changed": False}
        self._check_limits(pid, {rel: data}, max_bytes, max_files)
        self._write_files(pid, {rel: data})
        self.event("file added" if create else "file edited", author, ip, p, path=rel, bytes=len(data))
        return {"project": self._after_files_change(pid), "path": rel, "changed": True}

    def rename_file(self, pid: str, src: str, dst: str, author: str, ip: str) -> dict[str, Any]:
        p = self._writable(pid)
        a, b = self._draft_path(pid, src), self._draft_path(pid, dst)
        base = self.draft_dir(pid)
        if not a.is_file():
            raise SandboxError("No such file", 404)
        if b.exists():
            raise SandboxError(f"{b.relative_to(base).as_posix()} already exists", 409)
        b.parent.mkdir(parents=True, exist_ok=True)
        os.replace(a, b)
        self._prune_empty(a.parent, base)
        self.event("file renamed", author, ip, p, path=a.relative_to(base).as_posix(), to=b.relative_to(base).as_posix())
        return {"project": self._after_files_change(pid), "path": b.relative_to(base).as_posix()}

    def delete_file(self, pid: str, rel: str, author: str, ip: str) -> dict[str, Any]:
        p = self._writable(pid)
        path = self._draft_path(pid, rel)
        base = self.draft_dir(pid)
        if not path.is_file():
            raise SandboxError("No such file", 404)
        path.unlink()
        self._prune_empty(path.parent, base)
        self.event("file deleted", author, ip, p, path=path.relative_to(base).as_posix())
        return {"project": self._after_files_change(pid)}

    @staticmethod
    def _prune_empty(folder: Path, base: Path) -> None:
        while folder != base and base in folder.parents:
            try:
                folder.rmdir()
            except OSError:
                return
            folder = folder.parent

    # ------------------------------------------------------------------ versions
    def versions(self, pid: str) -> list[dict[str, Any]]:
        out = []
        for r in self._q("SELECT * FROM versions WHERE project_id=? ORDER BY number DESC", (pid,)):
            d = dict(r)
            d["manifest"] = json.loads(d["manifest"])
            out.append(d)
        return out

    def version(self, pid: str, number: int) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM versions WHERE project_id=? AND number=?", (pid, int(number)))
        if not rows:
            return None
        d = dict(rows[0])
        d["manifest"] = json.loads(d["manifest"])
        return d

    def latest(self, pid: str) -> dict[str, Any] | None:
        rows = self._q("SELECT number FROM versions WHERE project_id=? ORDER BY number DESC LIMIT 1", (pid,))
        return self.version(pid, rows[0]["number"]) if rows else None

    def publish(self, pid: str, note: str, author: str, ip: str, slug: str | None = None,
                pin: bool = False) -> dict[str, Any]:
        """Snapshot the draft as the next version: copied, hashed, made read-only. Links set to "latest" follow it.
        ``slug`` also creates a new link (pinned to this version when ``pin``)."""
        p = self._writable(pid)
        author = _clean_text(author, 60)
        if not author:
            raise SandboxError("Enter your name - it is recorded with the version")
        files = self.files(pid)
        if not files:
            raise SandboxError("Nothing to publish - add files first")
        if slug:
            slug, problem = check_slug(slug)
            if problem:
                raise SandboxError(problem)
            if self._q("SELECT 1 FROM links WHERE slug=?", (slug,)):
                raise SandboxError(f"'{slug}' is taken", 409)
        kind = p["kind"]
        entry = intake.entry_of([f["path"] for f in files]) if kind == "web" else None
        if kind == "web" and not entry:
            raise SandboxError("A web app needs an index.html (or set the kind to Script or Document)")
        with self._lock:
            last = self._q("SELECT MAX(number) AS n FROM versions WHERE project_id=?", (pid,))[0]["n"] or 0
            number = last + 1
            dest = self.version_dir(pid, number)
            if dest.exists():                                       # a half-written earlier attempt
                self._make_writable(dest)
                shutil.rmtree(dest)
            tmp = dest.with_name(dest.name + ".part~")
            if tmp.exists():
                self._make_writable(tmp)
                shutil.rmtree(tmp)
            src = self.draft_dir(pid)
            manifest = []
            for f in files:
                a = src / f["path"]
                b = tmp / f["path"]
                b.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(a, b, follow_symlinks=False)
                manifest.append({"path": f["path"], "size": b.stat().st_size, "sha256": _hash_file(b)})
            os.replace(tmp, dest)
            self._make_readonly(dest)
            total = sum(f["size"] for f in manifest)
            self._x("INSERT INTO versions(project_id, number, hash, size, files, manifest, kind, entry, allow_storage, "
                    "author, note, created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, number, overall_hash(manifest), total, len(manifest), json.dumps(manifest), kind, entry,
                     p["allow_storage"], author, str(note or "").strip()[:2000], time.time()))
            self._touch(pid)
            self.event("version published", author, ip, p, version=number, files=len(manifest), bytes=total,
                       hash=overall_hash(manifest), note=str(note or "").strip()[:200])
            if slug:
                self._create_link(p, slug, number if pin else None, author, ip)
        return self.version(pid, number) or {}

    @staticmethod
    def _make_readonly(root: Path) -> None:
        for dirpath, dirnames, filenames in os.walk(root, topdown=False):
            for n in filenames:
                os.chmod(os.path.join(dirpath, n), stat.S_IRUSR | stat.S_IRGRP)
            os.chmod(dirpath, stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)

    @staticmethod
    def _make_writable(root: Path) -> None:
        for dirpath, dirnames, filenames in os.walk(root):
            os.chmod(dirpath, 0o700)
            for n in filenames:
                os.chmod(os.path.join(dirpath, n), 0o600)

    def verify(self, pid: str, number: int) -> dict[str, Any]:
        """Recompute every hash of a version and compare it with the record made at publish."""
        v = self.version(pid, number)
        if not v:
            raise SandboxError("No such version", 404)
        base = self.version_dir(pid, number)
        bad, missing = [], []
        for f in v["manifest"]:
            p = base / f["path"]
            if not p.is_file():
                missing.append(f["path"])
            elif _hash_file(p) != f["sha256"]:
                bad.append(f["path"])
        extra = sorted({x["path"] for x in self._walk(base)} - {f["path"] for f in v["manifest"]})
        return {"version": number, "ok": not (bad or missing or extra), "changed": bad, "missing": missing,
                "extra": extra, "hash": v["hash"], "files": v["files"]}

    # ------------------------------------------------------------------ links
    def links(self, pid: str) -> list[dict[str, Any]]:
        out = []
        for r in self._q("SELECT * FROM links WHERE project_id=? ORDER BY created", (pid,)):
            d = dict(r)
            d["views"] = self._q("SELECT COALESCE(SUM(count), 0) AS n FROM views WHERE slug=?", (d["slug"],))[0]["n"]
            out.append(d)
        return out

    def link(self, slug: str) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM links WHERE slug=?", (str(slug).lower(),))
        return dict(rows[0]) if rows else None

    def slug_status(self, raw: str) -> dict[str, Any]:
        slug, problem = check_slug(raw)
        if problem:
            return {"slug": slug, "ok": False, "reason": problem}
        l = self.link(slug)
        if l:
            return {"slug": slug, "ok": False,
                    "reason": "taken" if l["status"] == "live" else "used before and taken down - link names are never reused"}
        return {"slug": slug, "ok": True, "reason": ""}

    def _create_link(self, p: dict[str, Any], slug: str, pin: int | None, author: str, ip: str) -> dict[str, Any]:
        try:
            self._x("INSERT INTO links(slug, project_id, pin, status, created, created_by) VALUES(?,?,?,'live',?,?)",
                    (slug, p["id"], pin, time.time(), _clean_text(author, 60) or "unknown"))
        except sqlite3.IntegrityError:
            raise SandboxError(f"'{slug}' is taken", 409)
        self.event("link created", author, ip, p, slug=slug, target=f"v{pin}" if pin else "latest")
        return self.link(slug) or {}

    def create_link(self, pid: str, raw: str, target: Any, author: str, ip: str) -> dict[str, Any]:
        p = self._writable(pid)
        if not _clean_text(author, 60):
            raise SandboxError("Enter your name - it is recorded with the link")
        slug, problem = check_slug(raw)
        if problem:
            raise SandboxError(problem)
        if not self.latest(pid):
            raise SandboxError("Publish a version first")
        pin = self._target(pid, target)
        with self._lock:
            st = self.slug_status(slug)
            if not st["ok"]:
                raise SandboxError(f"'{slug}' is {st['reason']}", 409)
            return self._create_link(p, slug, pin, author, ip)

    def _target(self, pid: str, target: Any) -> int | None:
        if target in (None, "", "latest"):
            return None
        try:
            n = int(str(target).lstrip("v"))
        except ValueError:
            raise SandboxError("target must be 'latest' or a version number")
        if not self.version(pid, n):
            raise SandboxError(f"There is no version {n}")
        return n

    def repoint(self, slug: str, target: Any, author: str, ip: str) -> dict[str, Any]:
        l = self.link(slug)
        if not l:
            raise SandboxError("No such link", 404)
        if l["status"] != "live":
            raise SandboxError("This link was taken down - link names are never reused", 409)
        p = self._writable(l["project_id"])
        pin = self._target(p["id"], target)
        if pin != l["pin"]:
            self._x("UPDATE links SET pin=? WHERE slug=?", (pin, l["slug"]))
            self.event("link repointed", author, ip, p, slug=l["slug"], before=f"v{l['pin']}" if l["pin"] else "latest",
                       after=f"v{pin}" if pin else "latest")
        return self.link(l["slug"]) or {}

    def retire(self, slug: str, author: str, ip: str) -> dict[str, Any]:
        l = self.link(slug)
        if not l:
            raise SandboxError("No such link", 404)
        if l["status"] == "retired":
            return l
        p = self.project(l["project_id"])
        self._x("UPDATE links SET status='retired', retired=?, retired_by=? WHERE slug=?",
                (time.time(), _clean_text(author, 60) or "unknown", l["slug"]))
        self.event("link retired", author, ip, p, slug=l["slug"])
        return self.link(l["slug"]) or {}

    # ------------------------------------------------------------------ records
    def events(self, pid: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if pid:
            rows = self._q("SELECT * FROM events WHERE project_id=? ORDER BY id DESC LIMIT ?", (pid, limit))
        else:
            rows = self._q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        return [{**dict(r), "detail": json.loads(r["detail"] or "{}")} for r in rows]

    def count_view(self, slug: str) -> None:
        day = time.strftime("%Y-%m-%d")
        self._x("INSERT INTO views(slug, day, count) VALUES(?,?,1) ON CONFLICT(slug, day) DO UPDATE SET count=count+1",
                (slug, day))

    def views_by_day(self, pid: str, days: int = 30) -> list[dict[str, Any]]:
        since = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
        return [dict(r) for r in self._q(
            "SELECT v.slug, v.day, v.count FROM views v JOIN links l ON l.slug=v.slug WHERE l.project_id=? AND v.day>=? "
            "ORDER BY v.day", (pid, since))]

    # ------------------------------------------------------------------ zips
    def zip_bytes(self, base: Path, manifest_paths: list[str], top: str) -> bytes:
        import io

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel in manifest_paths:
                p = base / rel
                if p.is_file() and not p.is_symlink():
                    zf.write(p, f"{top}/{rel}")
        return buf.getvalue()
