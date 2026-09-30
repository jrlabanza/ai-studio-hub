# AI Studio Hub

One home for your local AI tools — image, voice, video and music — with a GPU **auto-loader** that keeps an
8 GB card (or any card) working instead of running out of memory.

The hub starts each tool on demand in its own environment, puts a themed entrance in front of it, shares the GPU
between them, and gathers everything they make into one library. The tools themselves are **not modified**.

| # | Studio | Tool | Repository |
|---|---|---|---|
| 01 | **Image Studio** | Qwen Image Studio — Qwen-Image-2.1 text-to-image, editing, transparent layers | [jrlabanza/qwen-image-studio](https://github.com/jrlabanza/qwen-image-studio) |
| 02 | **Voice Studio** | Text To Speech generator — Qwen3-TTS preset voices, voice design, cloning, dubbing | [jrlabanza/tts-generator](https://github.com/jrlabanza/tts-generator) |
| 03 | **Video Studio** | Lumen Video Studio — LTX-2.5 and MiniMax H3 video with audio | [jrlabanza/video-generator](https://github.com/jrlabanza/video-generator) |
| 04 | **Music Studio** | Music Gen Studio — YuE2 songs from lyrics with an editable score | [jrlabanza/music-generator](https://github.com/jrlabanza/music-generator) |

## Quick start (Windows)

1. Set up the four tools once with their own scripts (`Initialize.bat` / `initialize.bat` in each folder). Put the
   folders either **inside** this repository's folder or **next to** it, with these names: `Image gen`, `qwen tts`,
   `video-gen`, `Yue2` (or point the hub at any folder in Settings → Studios → Folder).
2. Double-click **`Initialize AI Studio Hub.bat`** once. It creates a small `.venv` for the hub (FastAPI, uvicorn, httpx,
   websockets, psutil, Pillow — no PyTorch, no models).
3. Double-click **`Start AI Studio Hub.bat`**. The browser opens `http://127.0.0.1:7900`. Close the window to stop the hub
   *and* every studio it started.

Nothing starts until you need it: opening a studio starts it, pressing Generate loads its model.

```
your folder\
  ai-studio-hub\      this repository  (or the four tool folders inside it)
  Image gen\
  qwen tts\
  video-gen\
  Yue2\
```

## How the auto-loader works

Every request that needs the GPU (Generate, Enhance, Load model, Start engine, Sing, Transcribe …) passes through the hub
first. Before forwarding it the hub:

1. **waits** if another studio is still rendering — no job is ever interrupted, your request is queued and starts by itself;
2. **unloads** the other studios' models (Qwen-Image, Qwen3-TTS + Whisper + Chatterbox, the ComfyUI engine);
3. measures the free VRAM with `nvidia-smi`; if the card is still too full it **stops** the other studios' processes (an idle
   process still pins a few hundred MB of CUDA context — that matters on 8 GB) and asks a local **Ollama** to drop its models;
4. hands the GPU over and lets the studio load what it needs (queueing a model load ahead of the request where the tool
   does not do that itself).

Two policies, chosen automatically from the card:

| Card | Policy | Meaning |
|---|---|---|
| under 20 GB (8 / 12 / 16 GB) | **one model at a time** | strictly one studio holds the GPU; the others are unloaded and, if needed, stopped |
| 20 GB and up | **share when it fits** | small models (a 1.7B TTS model, YuE2) may stay resident next to each other as long as the measured free memory allows |

Also built in:

* **Load ahead** — a few seconds after you switch to a studio, its model is loaded and the previous one unloaded, so Generate is instant (Settings → Graphics card; off if you prefer).
* **Idle unload / idle stop** — models are unloaded after 8 minutes idle on an 8 GB card (15 / 30 on bigger ones) and idle studios are stopped later to free RAM. Pin a studio to keep it running.
* **Free GPU** — unload everything at once (for a game, another app, or peace of mind).
* Everything the hub does is listed under **Activity**: what was unloaded, stopped and how much memory came free.

## The shell

* **Home** — the card's live VRAM, a sparkline, the four studios with their model state, running jobs with progress, and the recent activity.
* **Studios** — each tool runs inside the shell (Alt+1 … Alt+4) with the hub's theme applied to its UI (Settings → Appearance to turn that off). Their own top-bar branding is replaced by the shell's; everything else is the original UI.
* **Library** — every image, clip, video and song the four studios have ever made, in one searchable grid with a viewer, downloads and "open folder". It reads the tools' output folders directly, so it works even when a studio is off.
* **Settings** — GPU policy and timers, appearance, network, per-studio folders / ports / autostart / pinning, and a check-up of what is installed.
* Light and dark themes; the theme is applied inside the studios too.

## Moved a tool folder? The hub repairs it

Tools store absolute paths. When you move a tool folder the hub fixes, on the tool's next start, what would otherwise
break: a stale `model_path` in Qwen Image Studio's `settings.json`, and stale "editable" installs (`pip install -e`) of
`qwen_tts` and `yue2_infer`, whose recorded source folders are pointed back at the moved folder. It also sets Lumen's
`engine_autostart` to `false` so the video engine only takes the GPU when a video is rendered. These are the only files the
hub writes inside the tool folders.

## Ports

| Service | Port | Notes |
|---|---|---|
| Hub / shell | 7900 | `--port N` or Settings → Network |
| Image Studio entrance | 7901 | proxies the backend on 7970 |
| Voice Studio entrance | 7902 | proxies the backend on 7861 (+ Chatterbox helper 7862) |
| Video Studio entrance | 7903 | proxies the backend on 8765 (ComfyUI engine 8188 stays internal) |
| Music Studio entrance | 7904 | proxies the backend on 7860 |

If a port is busy the hub moves to the next free one and prints the addresses in its window. A studio you started by hand
with its own launcher is adopted instead of started twice. Starting the hub twice opens the running copy.

### Sharing on your network

Settings → Network → *Everyone on my network* (or `--host 0.0.0.0`), restart the hub, then allow the ports once from an
**administrator** PowerShell:

```powershell
New-NetFirewallRule -DisplayName "AI Studio Hub" -Direction Inbound -Protocol TCP -LocalPort 7900-7904 -Action Allow -Profile Any
```

Anyone on the network can then use every studio without a password (the tools see the hub as a local visitor), so keep it
to networks you trust.

## How it is built

```
Start AI Studio Hub.bat        start the hub (accepts --port, --host, --no-browser)
Initialize AI Studio Hub.bat   one-time setup of the hub's .venv
hub\
  run.py            entry point: one uvicorn server on the hub port plus one port per studio
  main.py           the shell, its JSON API (/api/docs) and the live event stream
  proxy.py          per-studio reverse proxy: theme injection, GPU-route interception, lazy start, WebSocket pass-through
  orchestrator.py   the auto-loader: claims, waiting, unloading, escalation, idle policies
  process.py        the studio processes (job object, health checks, logs, adoption of hand-started copies)
  tools.py          everything the hub knows about each studio
  library.py        the unified library (reads the tools' output folders and indexes)
  gpu.py            nvidia-smi / psutil telemetry
  web\              the shell (no build step) and the theme files injected into the studios
data\               settings.json, logs\<tool>.log, thumbs\   (created on first start)
```

Requires Windows 10/11, Python 3.10+ (`py` launcher) and an NVIDIA GPU with a driver that provides `nvidia-smi`.
Linux is not supported yet (the process management is Windows-specific).

## License

MIT — see [LICENSE](LICENSE). The four tools and their models keep their own licenses.
