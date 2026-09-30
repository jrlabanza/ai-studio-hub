# AI Studio Hub

One home for your local AI tools — image, voice, video, music and Forge — with a GPU **auto-loader** that
keeps an 8 GB card (or any card) working instead of running out of memory.

The hub starts each tool on demand in its own environment, puts a themed entrance in front of it, shares the GPU
between them, and gathers everything they make into one library. The tools themselves are **not modified**.

| # | Studio | Tool | Repository |
|---|---|---|---|
| 01 | **Image Studio** | Qwen Image Studio — Qwen-Image-2.1 text-to-image, editing, transparent layers | [jrlabanza/qwen-image-studio](https://github.com/jrlabanza/qwen-image-studio) |
| 02 | **Voice Studio** | Text To Speech generator — Qwen3-TTS preset voices, voice design, cloning, dubbing | [jrlabanza/tts-generator](https://github.com/jrlabanza/tts-generator) |
| 03 | **Video Studio** | Lumen Video Studio — LTX-2.5 and MiniMax H3 video with audio | [jrlabanza/video-generator](https://github.com/jrlabanza/video-generator) |
| 04 | **Music Studio** | Music Gen Studio — YuE2 songs from lyrics with an editable score | [jrlabanza/music-generator](https://github.com/jrlabanza/music-generator) |
| 05 | **Forge Studio** *(optional)* | Forge Neo — the Jrlabanza Image Generator build of Stable Diffusion WebUI Forge | [jrlabanza/jrlabanza-image-generator-core](https://github.com/jrlabanza/jrlabanza-image-generator-core) |

All five are git submodules of this repository, pinned to tested versions. Forge is *optional*: its repository is
private (collaborators only), and a clone that cannot fetch it still runs with the four others - the hub leaves a
studio out of the shell when its folder is empty, and also picks up a checkout that sits *next to* the hub folder.

Runs on **Windows** (each tool in its own Python environment) and on **Linux** (each tool in its own Docker
container, the `linux/` packaging every studio ships).

## What it looks like

![Home - the card, the studios and what the hub did](docs/screenshots/home.png)

*Home: live VRAM, the studios with their model state, and the activity feed. Every studio shares the same design
language ([docs/design.md](docs/design.md)) and opens inside the shell - here Image Studio with the GPU handed to it,
and Music Studio:*

![Image Studio inside the shell](docs/screenshots/studio.png)

![Music Studio inside the shell](docs/screenshots/studio-music.png)

*Library: everything the studios have made, in one grid (filtered to videos here):*

![Library](docs/screenshots/library.png)

*Settings: the GPU policy and timers, and one row per studio:*

![Settings](docs/screenshots/settings.png)

## Quick start (Windows)

1. Clone the hub **with the studios** (they are git submodules, pinned to tested versions):

   ```
   git clone --recurse-submodules https://github.com/jrlabanza/ai-studio-hub.git
   ```

   Already cloned without them? `git submodule update --init --recursive` fetches them (`--recursive` also brings the
   upstream YuE repository that Music Studio embeds). Without access to the private Forge repository, initialise the
   others by name: `git submodule update --init --recursive "Image gen" "qwen tts" video-gen Yue2`.
2. Set each studio up once with its own script: `Image gen\Initialize.bat`, `qwen tts\initialize.bat`,
   `video-gen\initialize.bat`, `Yue2\initialize.bat`, `forge\initialize.bat`. They create their own Python environments
   and download their models.
3. Double-click **`Initialize AI Studio Hub.bat`** once. It creates a small `.venv` for the hub (FastAPI, uvicorn, httpx,
   websockets, psutil, Pillow — no PyTorch, no models).
4. Double-click **`Start AI Studio Hub.bat`**. The browser opens `http://127.0.0.1:7900`. Close the window to stop the hub
   *and* every studio it started.

Nothing starts until you need it: opening a studio starts it, pressing Generate loads its model.

## Quick start (Linux)

On Linux the studios are the Docker-packaged checkouts of the same repositories: every one has a `linux/` directory
(Dockerfile, `compose.yml`, `run.sh`, `stop.sh`, `initialize.sh`) and its own image `ai/<tool>:latest`. The hub finds
them **inside its folder** (the submodule layout) or **next to it** under any of their usual names — so the `~/AI`
layout used with [jrlabanza/ai-launcher](https://github.com/jrlabanza/ai-launcher) works as it is:

```
~/AI/
  ai-studio-hub/        this repo
  qwen-image-studio/    Image Studio      (ai/qwen-image)
  qwen-tts/             Voice Studio      (ai/qwen-tts + ai/chatterbox)
  video-generator/      Video Studio      (ai/video)
  yue2/                 Music Studio      (ai/yue2)
  forge/                Forge Studio      (ai/forge)
```

1. Set the studios up once with their own `linux/initialize.sh` (driver, Docker + NVIDIA Container Toolkit, image,
   models) — or `ai init <tool>` with the AI Launcher.
2. Run **`./Initialize AI Studio Hub.sh`** once (the same as `linux/initialize.sh`): it creates the hub's `.venv`, lists
   which studios it found and adds an app-menu entry, **AI – Studio Hub**.
3. Run **`./Start AI Studio Hub.sh`** (or `linux/run.sh`, or `ai run hub`). The browser opens `http://127.0.0.1:7900`.
   Ctrl+C or closing the window stops the hub and every container it started; `linux/stop.sh` does the same from
   elsewhere. `linux/run.sh --background` starts it detached, `linux/test.sh` checks the environment and does a real boot.

How the container path differs from the tools' own `run.sh`:

* The hub runs `docker compose up` itself (attached, so the container log streams into the hub) and **not**
  `run.sh`, because `run.sh` stops every other tool first — on an 8 GB card the hub prefers to *unload models* and
  only stops a container when the card is still too full. Several studios can therefore be up at once with their
  models unloaded (each idle container pins a few hundred MB of CUDA context, which the idle-stop timer reclaims).
* The hub's own additions to a studio's compose file live in `hub/compose/<service>.yml` and are merged with
  `-f`: Image Studio starts with `--no-autoload` and Voice Studio without `--preload`, so no model touches the GPU
  before the hub hands it over. The studios' `linux/` packaging is never edited.
* Container ports are fixed by the compose files (7864, 7861, 8765, 7863, 7860); the entrances on 7901–7905 and
  the hub on 7900 are the same as on Windows.
* A container started by hand (`ai run <tool>`) is taken over rather than duplicated; a container stopped by hand
  (`ai stop <tool>`) is shown as stopped, not as an error. Chatterbox comes up with Voice Studio when its image exists
  (Settings → Studios → Chatterbox).
* Forge loads its last checkpoint the moment its UI opens (its own behaviour). That request travels through Gradio's
  queue, which the hub treats as a GPU claim, so the other studios are unloaded first; and should a studio ever come
  up holding a model while another one owns the GPU, the hub unloads it again straight away.

## How the auto-loader works

Every request that needs the GPU (Generate, Enhance, Load model, Start engine, Sing, Transcribe, a Forge txt2img …) passes through the hub first. Before forwarding it the hub:

1. **waits** if another studio is still rendering — no job is ever interrupted, your request is queued and starts by itself;
2. **unloads** the other studios' models (Qwen-Image, Qwen3-TTS + Whisper + Chatterbox, the ComfyUI engine, Forge's
   checkpoint);
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

* **Home** — the card's live VRAM, a sparkline, the studios with their model state, running jobs with progress, and the recent activity.
* **Studios** — each tool runs inside the shell (Alt+1 … Alt+5). Every studio implements the same design language natively (see [docs/design.md](docs/design.md)), so it looks the same on its own and inside the hub; the shell only keeps the studio's light/dark theme in step with its own (Settings → Appearance) and hides the studio's own top-bar branding, since the shell shows its name.
* **Library** — every image, clip, video and song the studios have ever made, in one searchable grid with a viewer, downloads and "open folder". It reads the tools' output folders directly (Forge's prompts come from the PNG metadata), so it works even when a studio is off.
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
| Image Studio entrance | 7901 | proxies the backend on 7970 (Windows) / 7864 (Linux container) |
| Voice Studio entrance | 7902 | proxies the backend on 7861 (+ Chatterbox helper 7862) |
| Video Studio entrance | 7903 | proxies the backend on 8765 (ComfyUI engine 8188 stays internal) |
| Music Studio entrance | 7904 | proxies the backend on 7860 (Windows) / 7863 (Linux container) |
| Forge Studio entrance | 7905 | proxies the backend on 7866 (Windows) / 7860 (Linux container) |

If a port is busy the hub moves to the next free one and prints the addresses in its window. A studio you started by hand
with its own launcher is adopted instead of started twice. Starting the hub twice opens the running copy.

### Sharing on your network

Settings → Network → *Everyone on my network* (or `--host 0.0.0.0`), restart the hub, then allow the ports once.
Windows, from an **administrator** PowerShell:

```powershell
New-NetFirewallRule -DisplayName "AI Studio Hub" -Direction Inbound -Protocol TCP -LocalPort 7900-7905 -Action Allow -Profile Any
```

Linux with ufw: `sudo ufw allow 7900:7905/tcp`. Anyone on the network can then use every studio without a password
(the tools see the hub as a local visitor), so keep it to networks you trust.

## How it is built

```
Start AI Studio Hub.bat / .sh        start the hub (accepts --port, --host, --no-browser)
Initialize AI Studio Hub.bat / .sh   one-time setup of the hub's .venv
linux\                run.sh, stop.sh, initialize.sh, test.sh, install-desktop.sh - the same packaging
                      shape as the studios' linux/ folders, so `ai run|stop|init|test hub` work too
hub\
  run.py            entry point: one uvicorn server on the hub port plus one port per studio
  main.py           the shell, its JSON API (/api/docs) and the live event stream
  proxy.py          per-studio reverse proxy: theme injection, GPU-route interception, lazy start, WebSocket pass-through
  orchestrator.py   the auto-loader: claims, waiting, unloading, escalation, idle policies
  process.py        the studio processes: native (.venv) and docker backends, health checks, logs, adoption
  docker.py         docker / docker compose helpers for the Linux packaging
  tools.py          everything the hub knows about each studio
  library.py        the unified library (reads the tools' output folders and indexes)
  gpu.py            nvidia-smi / psutil telemetry
  compose\          the hub's compose overrides (start-idle switches) merged with the studios' compose files on Linux
  web\              the shell (no build step) and the theme files injected into the studios
data\               settings.json, logs\<tool>.log, thumbs\, hub.pid   (created on first start)
```

Requires Python 3.10+ and an NVIDIA GPU with a driver that provides `nvidia-smi`. Windows 10/11 needs the `py`
launcher; Linux needs Docker with the NVIDIA Container Toolkit (any studio's `linux/initialize.sh` installs them) and
`python3-venv`.

## License

MIT — see [LICENSE](LICENSE). The tools and their models keep their own licenses.
