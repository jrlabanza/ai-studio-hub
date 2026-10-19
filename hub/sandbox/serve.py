"""The Sandbox's content server: a separate, minimal Starlette app on its own port (7910 by default).

It serves Sandbox files and nothing else - no hub API, no studio, no docs page, GET/HEAD only:

    /<slug>/...                 a published link (its pinned version, or the latest)
    /<slug>@v<N>/...            version N of that link's project - a permanent address
    /_draft/<project>/<token>/  the draft, for the editor's preview (the token is unguessable, the hub UI gets it
                                from the hub API)

A web app's files are served as they are; a script or a document gets a read-only viewer page at its root, plus
``raw/<file>`` and ``download.zip``. Uploaded server-side code is never executed.

Every response carries ``Content-Security-Policy: sandbox ...`` without ``allow-same-origin`` (unless the project
allows storage), so each page runs in an opaque origin: it cannot read other apps' storage, and the browser labels
its requests ``Origin: null``. The policy also limits what the page may load or call to its own files and https -
plain-http requests to other hosts (the hub, the studios, other machines on the LAN) are blocked by the browser.
The hub's own ports refuse any request whose Origin is a Sandbox page (``blocks_origin``, used by run.Dispatcher),
so even a page that got past the policy cannot drive the hub.
"""
from __future__ import annotations

import html
import json
import mimetypes
import re
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from . import intake

if TYPE_CHECKING:
    from .store import Store

SANDBOX_FLAGS = "allow-scripts allow-forms allow-popups allow-modals allow-downloads"
TYPES = {".js": "text/javascript", ".mjs": "text/javascript", ".cjs": "text/javascript", ".css": "text/css",
         ".html": "text/html", ".htm": "text/html", ".json": "application/json", ".map": "application/json",
         ".svg": "image/svg+xml", ".wasm": "application/wasm", ".webp": "image/webp", ".avif": "image/avif",
         ".ico": "image/x-icon", ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf", ".otf": "font/otf",
         ".md": "text/plain", ".txt": "text/plain", ".csv": "text/csv", ".xml": "application/xml",
         ".webmanifest": "application/manifest+json", ".mp4": "video/mp4", ".webm": "video/webm",
         ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg", ".flac": "audio/flac", ".pdf": "application/pdf"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif", ".ico", ".bmp"}
VIEW_LIMIT = 400 * 1024                 # the viewer shows files up to this size; larger ones are download-only


def csp(allow_storage: bool, origin: str = "") -> str:
    own = f"'self' {origin}".strip()
    return (f"sandbox {SANDBOX_FLAGS}{' allow-same-origin' if allow_storage else ''}; "
            f"default-src {own} https: data: blob: 'unsafe-inline' 'unsafe-eval'; "
            f"form-action {own} https:; base-uri {own}")


def security_headers(allow_storage: bool, origin: str = "", draft: bool = False) -> dict[str, str]:
    h = {"Content-Security-Policy": csp(allow_storage, origin), "X-Content-Type-Options": "nosniff",
         "Referrer-Policy": "no-referrer", "Cross-Origin-Resource-Policy": "cross-origin",
         # an app in an opaque origin fetching its own data.json is a cross-origin request: let it read the answer
         # (no credentials - the sandbox sets no cookies)
         "Access-Control-Allow-Origin": "*"}
    if draft:
        h["Cache-Control"] = "no-store"
        h["X-Robots-Tag"] = "noindex, nofollow"
    else:
        h["Cache-Control"] = "no-cache"
    return h


def content_type(path: str, raw_text: bool = False) -> str:
    ext = intake.ext_of(path)
    if raw_text and ext not in IMAGE_EXT and ext not in (".pdf", ".mp4", ".webm", ".mp3", ".wav", ".ogg", ".flac"):
        return "text/plain; charset=utf-8"
    t = TYPES.get(ext) or mimetypes.guess_type("x" + ext)[0] or (
        "text/plain" if ext in intake.TEXT_EXT else "application/octet-stream")
    if t.startswith("text/") or t in ("application/json", "application/xml", "image/svg+xml"):
        t += "; charset=utf-8"
    return t


def blocks_origin(scope: dict, sandbox_ports: set[int], public_url: str = "") -> bool:
    """True for a request on a hub or studio port that comes from a Sandbox page: Origin ``null`` (an opaque,
    sandboxed page), an origin on the Sandbox port, or the Sandbox's public address."""
    origin = ""
    for k, v in scope.get("headers") or []:
        if k == b"origin":
            origin = v.decode("latin-1").strip().lower()
            break
    if not origin:
        return False
    if origin == "null":
        return True
    if public_url and origin == public_url.rstrip("/").lower():
        return True
    try:
        u = urlsplit(origin)
        port = u.port or (443 if u.scheme == "https" else 80)
    except ValueError:
        return True
    return port in sandbox_ports


# ---------------------------------------------------------------------- pages (our own HTML, on the sandbox origin)
PAGE_CSS = """
:root{--bg:#F4F1EF;--panel:#fff;--panel-2:#FAF7F5;--line:#E6DFDA;--text:#1F1A17;--muted:#7A6E68;--accent:#E05A4E;
--teal:#1C8C7E;--mono:"Cascadia Code",Consolas,"JetBrains Mono",ui-monospace,monospace;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--bg:#0F0D0C;--panel:#181513;--panel-2:#1F1B19;--line:rgba(255,255,255,.09);
--text:#F2ECE8;--muted:#9C8F88;--accent:#FF7B6C;--teal:#3CC2B0}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14.5px/1.45 Inter,"Segoe UI",system-ui,
-apple-system,sans-serif}a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:1180px;margin:0 auto;padding:28px 24px 60px}.eyebrow{font-size:11.5px;text-transform:uppercase;
letter-spacing:.14em;color:var(--muted);font-weight:700}h1{font-size:26px;margin:4px 0 6px}.lead{color:var(--muted);margin:0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px 20px}
.pill{display:inline-block;padding:3px 10px;border-radius:999px;background:var(--panel-2);border:1px solid var(--line);
font-size:12px;font-weight:700;color:var(--muted)}.pill.teal{color:var(--teal)}
.btn{display:inline-flex;align-items:center;gap:6px;padding:7px 13px;border-radius:9px;border:1px solid var(--line);
background:var(--panel);color:var(--text);font:inherit;font-weight:600;font-size:13px;cursor:pointer}
.btn:hover{background:var(--panel-2);text-decoration:none}.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
"""

VIEWER_CSS = PAGE_CSS + """
.head{display:flex;gap:20px;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;margin-bottom:16px}
.meta{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px;align-items:center;color:var(--muted);font-size:13px}
.desc{white-space:pre-wrap;margin:12px 0 0;max-width:80ch}.note{margin-top:10px;font-size:13px;color:var(--muted)}
.note b{color:var(--text)}.grid{display:grid;grid-template-columns:260px minmax(0,1fr);gap:16px;align-items:start}
@media (max-width:820px){.grid{grid-template-columns:1fr}}
.files{padding:8px}.files a{display:flex;justify-content:space-between;gap:8px;padding:6px 9px;border-radius:8px;
color:var(--text);font-size:13px;overflow:hidden}.files a span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.files a small{color:var(--muted);flex:none}.files a.on{background:var(--panel-2);font-weight:600;box-shadow:inset 3px 0 0 var(--accent)}
.files a:hover{text-decoration:none;background:var(--panel-2)}
.code-head{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:10px 14px;border-bottom:1px solid var(--line)}
.code-head b{font-family:var(--mono);font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.code{padding:0;overflow:hidden}.scroll{overflow:auto;max-height:78vh}
table.src{border-collapse:collapse;font-family:var(--mono);font-size:12.5px;line-height:1.55;width:100%}
table.src td{padding:0 14px;vertical-align:top;white-space:pre}table.src td.n{width:1%;text-align:right;color:var(--muted);
user-select:none;border-right:1px solid var(--line);padding-left:16px;opacity:.75}
.cell{border-top:1px solid var(--line);padding:10px 14px}.cell .tag{font-size:11px;color:var(--muted);font-weight:700;letter-spacing:.08em}
.cell pre{margin:6px 0 0;font-family:var(--mono);font-size:12.5px;white-space:pre-wrap}
.img{padding:20px;text-align:center}.img img{max-width:100%;max-height:70vh;background:repeating-conic-gradient(#8881 0 25%,transparent 0 50%) 0 0/16px 16px}
.banner{background:var(--panel-2);border:1px dashed var(--line);border-radius:10px;padding:8px 12px;font-size:13px;margin-bottom:14px;color:var(--muted)}
.foot{margin-top:26px;color:var(--muted);font-size:12.5px}
"""


def _fmt_size(n: int) -> str:
    return f"{n} B" if n < 1024 else (f"{n / 1024:.1f} KB" if n < 1048576 else f"{n / 1048576:.1f} MB")


def _fmt_date(ts: float | None) -> str:
    return time.strftime("%d %b %Y %H:%M", time.localtime(ts)) if ts else ""


def message_page(title: str, text: str, status: int, eyebrow: str = "AI Studio Hub Sandbox") -> Response:
    body = (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,"
            f"initial-scale=1'><title>{html.escape(title)}</title><style>{PAGE_CSS}.wrap{{max-width:620px;padding-top:12vh}}"
            f"</style></head><body><div class='wrap'><div class='card'><div class='eyebrow'>{html.escape(eyebrow)}</div>"
            f"<h1>{html.escape(title)}</h1><p class='lead'>{text}</p></div></div></body></html>")
    return Response(body, status_code=status, media_type="text/html; charset=utf-8")


def _source_table(text: str) -> str:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    rows = "".join(f"<tr><td class='n'>{i}</td><td>{html.escape(l) or ' '}</td></tr>" for i, l in enumerate(lines, 1))
    return f"<table class='src'>{rows}</table>"


def _notebook(data: bytes) -> str | None:
    """A read-only rendering of a Jupyter notebook: markdown cells as text, code cells as code (no outputs run)."""
    try:
        nb = json.loads(data.decode("utf-8"))
        cells = nb.get("cells") or []
    except Exception:
        return None
    out = []
    for c in cells[:500]:
        src = c.get("source")
        src = "".join(src) if isinstance(src, list) else str(src or "")
        kind = c.get("cell_type", "code")
        out.append(f"<div class='cell'><div class='tag'>{html.escape(kind.upper())}</div><pre>{html.escape(src)}</pre></div>")
    return "".join(out) or "<div class='cell'>An empty notebook.</div>"


def viewer_page(p: dict[str, Any], v: dict[str, Any] | None, base: Path, files: list[dict[str, Any]], sel: str,
                draft: bool, slug: str = "") -> Response:
    """The read-only page a script or document link opens on: what it is, who made it, its files with line numbers,
    copy and download. Built here (escaped), served with the same sandbox policy as everything else."""
    paths = [f["path"] for f in files]
    if sel not in paths:
        readme = next((x for x in paths if x.lower() in ("readme.md", "readme.txt", "readme")), None)
        code = next((x for x in paths if intake.ext_of(x) in intake.CODE_EXT), None)
        sel = (code or readme if p["kind"] == "script" else readme or code) or (paths[0] if paths else "")
    file_list = "".join(
        f"<a href='?file={quote(f['path'])}' class='{'on' if f['path'] == sel else ''}' title='{html.escape(f['path'])}'>"
        f"<span>{html.escape(f['path'])}</span><small>{_fmt_size(f['size'])}</small></a>" for f in files)
    body = "<div class='img'>This project has no files.</div>"
    if sel:
        fp = base / sel
        size = fp.stat().st_size if fp.is_file() else 0
        ext = intake.ext_of(sel)
        raw = "raw/" + quote(sel)
        head = (f"<div class='code-head'><b title='{html.escape(sel)}'>{html.escape(sel)}</b><span>"
                f"<button class='btn' id='copy' type='button'>Copy</button> "
                f"<a class='btn' href='{raw}' download='{html.escape(sel.rsplit('/', 1)[-1])}'>Download</a></span></div>")
        if ext in IMAGE_EXT:
            body = head + f"<div class='img'><img src='{raw}' alt='{html.escape(sel)}'></div>"
            head = ""
        elif size > VIEW_LIMIT:
            body = head + f"<div class='img'>{_fmt_size(size)} - too large to show here. Download it to read it.</div>"
        else:
            data = fp.read_bytes() if fp.is_file() else b""
            if not intake.is_text(sel, data):
                body = head + f"<div class='img'>A binary file ({_fmt_size(size)}). Download it to open it.</div>"
            else:
                nb = _notebook(data) if ext == ".ipynb" else None
                text = data.decode("utf-8", errors="replace")
                body = head + f"<div class='scroll'>{nb or _source_table(text)}</div>" + (
                    f"<textarea id='src' hidden>{html.escape(text)}</textarea>")
    kind = {"script": "Script", "document": "Document", "web": "Web app"}.get(p["kind"], p["kind"])
    who = html.escape((v or {}).get("author") or p["author"])
    ver = f"v{v['number']}" if v else "draft"
    note = (v or {}).get("note") or ""
    title = html.escape(p["name"])
    banner = ("<div class='banner'>Draft preview - what the editor holds right now. Publish to share it.</div>" if draft else "")
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{VIEWER_CSS}</style></head><body><div class="wrap">{banner}
<div class="head"><div><div class="eyebrow">AI Studio Hub Sandbox · {html.escape(kind)}</div><h1>{title}</h1>
<div class="meta"><span class="pill teal">{html.escape(ver)}</span><span>by <b>{who}</b></span>
<span>published {_fmt_date((v or {}).get('created')) or '—'}</span><span>· created {_fmt_date(p['created'])}</span>
{f"<span>· {(v or {}).get('files')} files · {_fmt_size((v or {}).get('size') or 0)}</span>" if v else ""}</div>
{f"<p class='desc'>{html.escape(p['description'])}</p>" if p['description'] else ""}
{f"<div class='note'>Version note: <b>{html.escape(note)}</b></div>" if note else ""}</div>
<div><a class="btn primary" href="download.zip">Download all (.zip)</a></div></div>
<p class="lead" style="margin:0 0 14px">Scripts are shared to read and download, not run on the server.</p>
<div class="grid"><nav class="card files">{file_list or "<span class='lead'>No files.</span>"}</nav>
<section class="card code">{body}</section></div>
<div class="foot">{f"Version hash sha256 {html.escape(v['hash'][:16])}… · " if v else ""}{"link /" + html.escape(slug) if slug else ""}</div>
</div><script>
(function(){{var b=document.getElementById("copy"),s=document.getElementById("src");if(!b)return;if(!s){{b.hidden=true;return;}}
b.onclick=function(){{var t=s.value;function ok(){{b.textContent="Copied";setTimeout(function(){{b.textContent="Copy";}},1500);}}
function old(){{var a=document.createElement("textarea");a.value=t;document.body.appendChild(a);a.select();
try{{document.execCommand("copy");ok();}}catch(e){{b.textContent="Select and copy";}}a.remove();}}
if(navigator.clipboard&&window.isSecureContext){{navigator.clipboard.writeText(t).then(ok,old);}}else old();}};}})();
</script></body></html>"""
    return Response(page, media_type="text/html; charset=utf-8")


def build_sandbox_app(store: "Store") -> Starlette:
    """The app bound to the sandbox port. Only GET/HEAD of Sandbox files; every response gets the sandbox headers."""

    def origin_of(request: Request) -> str:
        host = request.headers.get("host", "")
        return f"{request.url.scheme}://{host}" if re.fullmatch(r"[A-Za-z0-9.\-\[\]:]+", host or "") else ""

    def finish(resp: Response, request: Request, allow_storage: bool = False, draft: bool = False) -> Response:
        for k, v in security_headers(allow_storage, origin_of(request), draft).items():
            resp.headers[k] = v
        return resp

    def not_found(request: Request, what: str = "Nothing at this address.") -> Response:
        return finish(message_page("Not found", html.escape(what), 404), request)

    def safe_file(base: Path, rel: str) -> Path | None:
        if not rel or rel.endswith("/"):
            return None
        try:
            clean = intake.clean_path(rel)
        except intake.IntakeError:
            return None
        if clean is None:
            return None
        p = base / clean
        cur = base
        for part in clean.split("/"):
            cur = cur / part
            if cur.is_symlink():
                return None
        try:
            if base.resolve() not in p.resolve().parents:
                return None
        except OSError:
            return None
        return p if p.is_file() else None

    def serve(request: Request, p: dict[str, Any], v: dict[str, Any] | None, base: Path, rest: str, draft: bool,
              slug: str = "") -> Response:
        """One project's files: ``rest`` is the path below the link (\"\" = its front page)."""
        kind = v["kind"] if v else p["kind"]
        storage = bool(p["allow_storage"]) and (draft or bool(v and v["allow_storage"]))
        files = v["manifest"] if v else store.files(p["id"])
        paths = [f["path"] for f in files]
        if kind == "web":
            entry = (v or {}).get("entry") or intake.entry_of(paths)
            rel = rest
            if rel == "" or rel.endswith("/"):
                rel = rel + "index.html" if rel else (entry or "index.html")
            fp = safe_file(base, rel)
            if fp is None and rel.endswith("/index.html"):
                fp = safe_file(base, rel[:-10] + "index.htm")
            if fp is None and f"{rel}/index.html" in paths:          # a folder without its slash: as a server would
                q = f"?{request.url.query}" if request.url.query else ""
                return finish(RedirectResponse(quote(rel.rsplit("/", 1)[-1]) + "/" + q, status_code=308), request)
            if fp is None or (rel not in paths and fp.relative_to(base).as_posix() not in paths):
                return not_found(request, "This app has no file at that address.")
            if slug and rest == "":
                store.count_view(slug)
            return finish(FileResponse(fp, media_type=content_type(rel)), request, storage, draft)
        # script / document: viewer, raw/<file>, download.zip
        if rest == "":
            if slug:
                store.count_view(slug)
            return finish(viewer_page(p, v, base, files, request.query_params.get("file", ""), draft, slug), request,
                          False, draft)
        if rest.startswith("raw/"):
            rel = rest[4:]
            fp = safe_file(base, rel)
            if fp is None or rel not in paths:
                return not_found(request, "No such file in this project.")
            return finish(FileResponse(fp, media_type=content_type(rel, raw_text=True)), request, False, draft)
        if rest == "download.zip":
            top = re.sub(r"[^A-Za-z0-9._-]+", "-", slug or p["name"]).strip("-") or "project"
            name = f"{top}-v{v['number']}" if v else f"{top}-draft"
            data = store.zip_bytes(base, paths, name)
            resp = Response(data, media_type="application/zip",
                            headers={"Content-Disposition": f'attachment; filename="{name}.zip"'})
            return finish(resp, request, False, draft)
        return not_found(request)

    async def handle(request: Request) -> Response:
        path = request.url.path
        if path == "/":
            return finish(message_page("Sandbox", "Projects people in your organisation built and shared. Open the link "
                                       "you were given - each project has its own address.", 200), request)
        if path == "/robots.txt":
            return finish(PlainTextResponse("User-agent: *\nDisallow: /\n"), request)
        if path == "/favicon.ico":
            return finish(Response(status_code=204), request)
        head, _, rest = path.lstrip("/").partition("/")
        has_slash = "/" in path.lstrip("/")
        if head == "_draft":
            pid, _, rest2 = rest.partition("/")
            token, slash2, rest3 = rest2.partition("/")
            try:
                p = store.project(pid)
            except Exception:
                return not_found(request)
            if not token or not secrets.compare_digest(token, p["preview_token"]):
                return not_found(request)
            if not slash2:
                return finish(RedirectResponse(f"{token}/", status_code=307), request, draft=True)
            return await _threaded(serve, request, p, None, store.draft_dir(pid), rest3, True)
        m = re.fullmatch(r"([a-z0-9][a-z0-9-]{0,46}[a-z0-9])(?:@v(\d{1,6}))?", head)
        if not m:
            return not_found(request)
        slug, vnum = m.group(1), m.group(2)
        link = store.link(slug)
        if not link:
            return not_found(request, f"There is no Sandbox link called “{slug}”.")
        try:
            p = store.project(link["project_id"], allow_deleted=True)
        except Exception:
            return not_found(request)
        if link["status"] != "live" or p["status"] == "deleted":
            return finish(message_page("This link was taken down",
                                       f"“{html.escape(slug)}” was taken down on {_fmt_date(link.get('retired') or p['updated'])}. "
                                       "Link names are never reused, so this address will not show anything else.", 410),
                          request)
        if not has_slash:
            q = f"?{request.url.query}" if request.url.query else ""
            return finish(RedirectResponse(f"/{quote(head)}/{q}", status_code=308), request)
        number = int(vnum) if vnum else (link["pin"] or (store.latest(p["id"]) or {}).get("number"))
        v = store.version(p["id"], number) if number else None
        if not v:
            return not_found(request, "That version does not exist." if vnum else "Nothing has been published here yet.")
        return await _threaded(serve, request, p, v, store.version_dir(p["id"], v["number"]), rest, False, slug)

    async def _threaded(fn, *args):
        from starlette.concurrency import run_in_threadpool

        return await run_in_threadpool(fn, *args)

    async def other_methods(request: Request) -> Response:
        return finish(PlainTextResponse("Only GET and HEAD are served here.", status_code=405,
                                        headers={"Allow": "GET, HEAD"}), request)

    return Starlette(routes=[Route("/{path:path}", handle, methods=["GET", "HEAD"]),
                             Route("/{path:path}", other_methods, methods=["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])])
