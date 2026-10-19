"""What may go into a Sandbox project: file paths, uploads (files, folders, a .zip) and the kind of project.

Everything here is pure (no database, no hub): it turns untrusted bytes into a clean ``{path: bytes}`` map or raises
``IntakeError`` with a message the shell shows as it is. The rules:

* a path is relative, ``/``-separated, at most 12 folders deep and 200 characters; no ``..``, no absolute or drive
  paths, no control characters or ``<>:"|?*\\``; ``__MACOSX/``, ``.git/``, ``.DS_Store`` and ``Thumbs.db`` are dropped;
* native executables and installers (.exe, .dll, .msi, ...) are refused - scripts are shared as text;
* a .zip is checked before anything is extracted: no symlinks, no encrypted entries, the *declared* uncompressed size
  within the limit and no entry compressed more than 200:1 (zip bombs); while extracting, the *actual* bytes are
  counted again, so a lying header cannot get past the limit either;
* a single top-level folder (``my-site/index.html`` ...) is stripped from a zip or a dropped folder;
* a lone .html file becomes ``index.html``.

The browser sends uploads as one "bundle" (no multipart parser needed, so no extra dependency): a 4-byte big-endian
header length, a JSON header ``{"files": [{"path", "size"}, ...], ...fields}``, then every file's bytes in order.
"""
from __future__ import annotations

import codecs
import io
import json
import posixpath
import re
import stat
import struct
import zipfile
import zlib
from typing import Any

MAX_DEPTH = 12
MAX_PATH = 200
MAX_HEADER = 256 * 1024
MAX_ZIP_ENTRIES = 5000
MAX_RATIO = 200                     # compressed:uncompressed beyond this (for entries over 1 MB) is treated as a bomb
BAD_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"|?*\\]')
DROP_PARTS = {"__MACOSX", ".git", ".svn", ".hg"}
DROP_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
BLOCKED_EXT = {".exe", ".dll", ".msi", ".msix", ".com", ".scr", ".sys", ".so", ".dylib", ".app", ".dmg", ".pkg",
               ".deb", ".rpm", ".apk", ".appimage", ".cpl", ".lnk"}
CODE_EXT = {".py", ".ipynb", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".sh", ".bash", ".zsh", ".ps1", ".psm1",
            ".bat", ".cmd", ".rb", ".go", ".rs", ".java", ".kt", ".c", ".h", ".cpp", ".hpp", ".cc", ".cs", ".php",
            ".r", ".sql", ".lua", ".pl", ".swift", ".scala", ".dart", ".vb", ".vbs", ".gs", ".m", ".jl"}
TEXT_EXT = CODE_EXT | {".html", ".htm", ".css", ".scss", ".less", ".json", ".md", ".markdown", ".txt", ".csv",
                       ".tsv", ".xml", ".svg", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env", ".log",
                       ".rst", ".tex", ".vue", ".svelte", ".astro", ".gitignore", ".editorconfig", ".lock"}
KINDS = ("web", "script", "document")


class IntakeError(ValueError):
    """An upload or a path the Sandbox refuses; the message is shown to the user."""


def clean_path(raw: str) -> str | None:
    """A safe relative path, or None for a file that is silently dropped (macOS / git clutter). Raises on anything
    that tries to leave the project folder."""
    p = str(raw or "").replace("\\", "/").strip()
    if not p:
        raise IntakeError("A file has no name")
    if p.startswith("/") or re.match(r"^[A-Za-z]:", p) or p.startswith("~"):
        raise IntakeError(f"Absolute paths are not allowed: {raw!r}")
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if any(x == ".." for x in parts):
        raise IntakeError(f"Paths may not climb out of the project (..): {raw!r}")
    if not parts:
        raise IntakeError(f"Not a file name: {raw!r}")
    if any(x in DROP_PARTS for x in parts[:-1]) or parts[-1] in DROP_NAMES or parts[-1] in DROP_PARTS:
        return None
    for x in parts:
        if BAD_CHARS.search(x) or x.endswith((" ", ".")) and x not in (".",):
            raise IntakeError(f"Unsupported character in {raw!r}")
    if len(parts) > MAX_DEPTH:
        raise IntakeError(f"Folders nested too deep: {raw!r}")
    out = "/".join(parts)
    if len(out) > MAX_PATH:
        raise IntakeError(f"Path too long (over {MAX_PATH} characters): {out[:60]}…")
    if ext_of(out) in BLOCKED_EXT:
        raise IntakeError(f"{parts[-1]}: programs and installers are not accepted - share the source code instead")
    return out


def ext_of(path: str) -> str:
    name = path.rsplit("/", 1)[-1].lower()
    if name.startswith(".") and name.count(".") == 1:
        return name                       # .gitignore, .env
    return posixpath.splitext(name)[1]


def is_text(path: str, data: bytes) -> bool:
    """Editable in the browser: a known text extension or UTF-8 without NUL bytes."""
    if b"\x00" in data[:8192]:
        return False
    if ext_of(path) in TEXT_EXT:
        return True
    try:
        codecs.getincrementaldecoder("utf-8")().decode(data[:65536], final=len(data) <= 65536)
        return True
    except UnicodeDecodeError:
        return False


def detect_kind(paths: list[str]) -> str:
    """index.html (or index.htm) at the top -> a web app; any code file -> a script; anything else -> a document."""
    low = {p.lower() for p in paths}
    if "index.html" in low or "index.htm" in low:
        return "web"
    if any(ext_of(p) in CODE_EXT for p in paths):
        return "script"
    return "document"


def entry_of(paths: list[str]) -> str | None:
    """The page a web app opens on: index.html, else the first top-level .html."""
    by_low = {p.lower(): p for p in paths}
    for name in ("index.html", "index.htm"):
        if name in by_low:
            return by_low[name]
    tops = sorted(p for p in paths if "/" not in p and ext_of(p) in (".html", ".htm"))
    return tops[0] if tops else None


def strip_common_folder(files: dict[str, bytes]) -> dict[str, bytes]:
    """my-site/index.html, my-site/app.js -> index.html, app.js (only when *every* file is inside one folder)."""
    if not files:
        return files
    firsts = {p.split("/", 1)[0] for p in files}
    if len(firsts) == 1 and all("/" in p for p in files):
        return {p.split("/", 1)[1]: d for p, d in files.items()}
    return files


def parse_bundle(body: bytes) -> tuple[dict[str, Any], list[tuple[str, bytes]]]:
    """Split the browser's upload bundle into its header fields and (path, bytes) pairs - raw paths, not cleaned yet."""
    if len(body) < 4:
        raise IntakeError("Empty upload")
    (hlen,) = struct.unpack(">I", body[:4])
    if hlen > MAX_HEADER or 4 + hlen > len(body):
        raise IntakeError("Malformed upload")
    try:
        head = json.loads(body[4:4 + hlen].decode("utf-8"))
    except Exception:
        raise IntakeError("Malformed upload header")
    if not isinstance(head, dict) or not isinstance(head.get("files", []), list):
        raise IntakeError("Malformed upload header")
    out: list[tuple[str, bytes]] = []
    pos = 4 + hlen
    for f in head.get("files", []):
        try:
            size = int(f.get("size"))
            path = str(f.get("path"))
        except Exception:
            raise IntakeError("Malformed upload header")
        if size < 0 or pos + size > len(body):
            raise IntakeError("The upload is shorter than it says - try again")
        out.append((path, body[pos:pos + size]))
        pos += size
    if pos != len(body):
        raise IntakeError("The upload is longer than it says - try again")
    return head, out


def _zip_is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    return info.create_system == 3 and stat.S_ISLNK(mode)


def expand_zip(data: bytes, max_bytes: int, max_files: int) -> dict[str, bytes]:
    """The files of a .zip, after every check in the module docstring. The single top-level folder is stripped."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise IntakeError("That .zip file is damaged or not a zip")
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        raise IntakeError(f"The zip has {len(infos)} entries - at most {MAX_ZIP_ENTRIES} are read")
    declared = 0
    entries: list[tuple[zipfile.ZipInfo, str]] = []
    for info in infos:
        if info.flag_bits & 0x1:
            raise IntakeError("Password-protected zips are not accepted")
        if _zip_is_symlink(info):
            raise IntakeError(f"The zip contains a symbolic link ({info.filename}) - links are not accepted")
        if info.is_dir():
            clean_path(info.filename.rstrip("/") + "/x")      # a hostile folder name still fails here
            continue
        path = clean_path(info.filename)
        if path is None:
            continue
        declared += info.file_size
        if declared > max_bytes:
            raise IntakeError(f"The zip unpacks to more than {max_bytes // 1048576} MB")
        if info.file_size > 1024 * 1024 and info.file_size > MAX_RATIO * max(1, info.compress_size):
            raise IntakeError(f"{path} is compressed {info.file_size // max(1, info.compress_size)}:1 - "
                              "that looks like a zip bomb")
        entries.append((info, path))
    if len(entries) > max_files:
        raise IntakeError(f"The zip has {len(entries)} files - a project holds at most {max_files}")
    files: dict[str, bytes] = {}
    total = 0
    for info, path in entries:
        buf = bytearray()
        try:
            with zf.open(info) as fh:
                while True:
                    chunk = fh.read(1024 * 256)
                    if not chunk:
                        break
                    buf += chunk
                    total += len(chunk)
                    if len(buf) > info.file_size or total > max_bytes:
                        raise IntakeError("The zip holds more data than its directory says - refused")
        except IntakeError:
            raise
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError, OSError, zlib.error) as exc:
            raise IntakeError(f"{path} in the zip cannot be read ({exc.__class__.__name__}) - zip it again")
        if path in files:
            raise IntakeError(f"The zip has {path} twice")
        files[path] = bytes(buf)
    return strip_common_folder(files)


def intake(pairs: list[tuple[str, bytes]], max_bytes: int, max_files: int, creating: bool) -> dict[str, bytes]:
    """Validated files of one upload. A lone .zip is unpacked; when creating a project, one shared top folder is
    stripped and a lone .html file becomes index.html."""
    if not pairs:
        raise IntakeError("Choose at least one file")
    if sum(len(d) for _, d in pairs) > max_bytes:
        raise IntakeError(f"The upload is larger than {max_bytes // 1048576} MB")
    if len(pairs) == 1 and pairs[0][0].lower().endswith(".zip"):
        return expand_zip(pairs[0][1], max_bytes, max_files)
    files: dict[str, bytes] = {}
    for raw, data in pairs:
        path = clean_path(raw)
        if path is None:
            continue
        if path in files:
            raise IntakeError(f"{path} is in the upload twice")
        files[path] = data
    if len(files) > max_files:
        raise IntakeError(f"{len(files)} files - a project holds at most {max_files}")
    if creating:
        files = strip_common_folder(files)
        if len(files) == 1:
            (only, data), = files.items()
            if ext_of(only) in (".html", ".htm") and "/" not in only:
                files = {"index.html": data}
    if not files:
        raise IntakeError("Nothing to add (only system files such as .DS_Store were in the upload)")
    return files
