"""Serving a media file with HTTP Range support (audio and video seek) - used by the shell and the entrances."""
from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

mimetypes.add_type("font/otf", ".otf")
mimetypes.add_type("font/ttf", ".ttf")
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("audio/flac", ".flac")


def ranged_file(request: Request, path: Path, download: bool = False) -> Response:
    """FileResponse with HTTP Range support so audio and video can seek."""
    size = path.stat().st_size
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{path.name}"'
    rng = request.headers.get("range")
    if not rng or not rng.startswith("bytes="):
        return FileResponse(path, media_type=ctype, headers=headers)
    try:
        start_s, end_s = rng[6:].split("-", 1)
        start = int(start_s) if start_s else 0
        end = int(end_s) if end_s else size - 1
    except ValueError:
        raise HTTPException(416, "bad range")
    if start >= size:
        raise HTTPException(416, "range not satisfiable")
    end = min(end, size - 1)
    length = end - start + 1
    headers.update({"Content-Range": f"bytes {start}-{end}/{size}", "Content-Length": str(length)})

    def body():
        with open(path, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(1024 * 512, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(body(), status_code=206, media_type=ctype, headers=headers)
