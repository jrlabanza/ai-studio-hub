"""Start AI Studio Hub.

    python -m hub [--host 127.0.0.1] [--port 7900] [--no-browser]

One uvicorn server listens on the hub port *and* on one extra port per tool; a tiny ASGI
dispatcher routes each connection to the shell or to the right tool proxy by the port it arrived
on. One process, one event loop, one place that knows what the GPU is doing.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import webbrowser
from typing import Any

from . import __version__
from .config import APP_NAME, PID_FILE, ROOT, ensure_dirs, load_settings


class Dispatcher:
    """Routes ASGI connections to an app by the local port they were accepted on."""

    def __init__(self, default: Any, by_port: dict[int, Any]) -> None:
        self.default = default
        self.by_port = by_port

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.default(scope, receive, send)
        server = scope.get("server")
        app = self.by_port.get(server[1], self.default) if server else self.default
        return await app(scope, receive, send)


def _port_answers(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as c:
        c.settimeout(0.3)
        return c.connect_ex(("127.0.0.1", port)) == 0


def _running_hub(port: int) -> bool:
    """True when a hub already answers on the port."""
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=2) as r:
            data = json.loads(r.read().decode("utf-8"))
        return isinstance(data, dict) and (data.get("app") or {}).get("name") == APP_NAME
    except Exception:
        return False


def bind(host: str, preferred: int, tries: int = 20) -> tuple[socket.socket, int]:
    """Bind a listening socket with exclusive address use, moving to the next port when busy."""
    bind_host = "0.0.0.0" if host in ("0.0.0.0", "", "*") else host
    last: Exception | None = None
    for p in range(preferred, preferred + tries):
        if _port_answers(p):
            last = OSError(f"port {p} already answers")
            continue
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((bind_host, p))
            s.listen(256)
            s.setblocking(False)
            return s, p
        except OSError as exc:
            last = exc
            s.close()
    raise SystemExit(f"No free port near {preferred} ({last})")


def main() -> None:
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--host", default=None, help="interface to listen on (default from settings, 127.0.0.1)")
    ap.add_argument("--port", type=int, default=None, help="hub port (default from settings, 7900)")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--log-level", default="warning")
    args = ap.parse_args()

    os.chdir(ROOT)
    ensure_dirs()
    settings = load_settings()
    host = args.host or settings.bind_host or "127.0.0.1"
    port = args.port or settings.hub_port

    import uvicorn

    from .core import Hub
    from .main import create_app
    from .proxy import build_proxy_app

    # Already running (double-clicked twice)? Open that one instead of starting a second hub on other ports.
    running = _running_hub(port)
    if running:
        url = f"http://127.0.0.1:{port}"
        print(f"\n  {APP_NAME} is already running at {url} - opening it instead of starting a second copy.\n")
        if not args.no_browser:
            webbrowser.open(url)
        return

    hub = Hub()
    hub_sock, hub_port = bind(host, port)
    hub.hub_port = hub_port
    socks = [hub_sock]
    by_port: dict[int, Any] = {}
    for tool in hub.tools.values():
        if not tool.enabled:
            continue
        want = int(tool.cfg.get("proxy_port") or 0) or (hub_port + 1)
        s, p = bind(host, want)
        hub.proxy_ports[tool.id] = p
        by_port[p] = build_proxy_app(tool, hub)
        socks.append(s)

    hub_app = create_app(hub)
    dispatcher = Dispatcher(hub_app, by_port)
    config = uvicorn.Config(dispatcher, host=host, port=hub_port, log_level=args.log_level, access_log=False,
                            ws_ping_interval=20, ws_ping_timeout=20, timeout_keep_alive=75, lifespan="on")
    server = uvicorn.Server(config)

    browse_host = "127.0.0.1" if host in ("0.0.0.0", "", "*") else host
    url = f"http://{browse_host}:{hub_port}"
    print(f"\n  {APP_NAME} {__version__}")
    print(f"  Shell     : {url}")
    for tool in hub.tools.values():
        if tool.id in hub.proxy_ports:
            print(f"  {tool.spec.name:<13}: http://{browse_host}:{hub.proxy_ports[tool.id]}   (backend on 127.0.0.1:{tool.cfg.get('port')})")
    if host in ("0.0.0.0", "", "*"):
        print("  Listening on every interface - people on your network can use it (see README for the firewall rule).")
    print("  Close this window or press Ctrl+C to stop everything.\n", flush=True)
    print(f"  Studios run {'in their Linux containers (docker compose)' if sys.platform.startswith('linux') else 'in their own Python environments'}.\n", flush=True)

    if not args.no_browser and settings.open_browser:
        def _open() -> None:
            for _ in range(600):
                if server.started:
                    break
                if server.should_exit:
                    return
                time.sleep(0.1)
            else:
                return
            try:
                webbrowser.open(url)
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()

    if sys.platform != "win32":
        import signal

        # Closing the terminal window sends SIGHUP: treat it like Ctrl+C so the studios are stopped too.
        signal.signal(signal.SIGHUP, lambda *_: server.handle_exit(signal.SIGTERM, None))
    try:
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
    try:
        server.run(sockets=socks)
    finally:
        try:
            if PID_FILE.is_file() and PID_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
                PID_FILE.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()
