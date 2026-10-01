# Models - one page for every studio's models

The **Models** page (Alt+M) lists what each studio has installed, what it can download, and which
model it is using - and lets you download, redownload, verify, delete and switch from the hub, so
a newer model never means hunting through five folders.

## Two kinds of studio

| studio | how the hub works with it |
|---|---|
| Image Studio, Music Studio, Forge | **folders and files on disk.** The hub scans the studio's model folders, shows the known catalogue next to what is installed, downloads straight into the right folder and tells the studio which model to use through its own API. |
| Voice Studio, Video Studio | **delegated.** These studios already have a catalogue and a downloader of their own (Voice: the HuggingFace cache it fills on load; Video: its model packs with resumable downloads). The hub shows that catalogue and forwards download / cancel / delete to the studio. The studio has to be running for its list to appear. |

What each studio exposes is declared in `hub/tools.py` (`ToolSpec.model_kinds`, `model_delegate`,
`model_note`); the registry, scanners and download manager are `hub/models.py`.

| studio | kinds | selectable ("Use") |
|---|---|---|
| Image Studio | `model` - diffusers folders in `models/` (catalogue: Qwen-Image-2.1 and the two prompt enhancers); `lora` - `.safetensors` in `loras/` | model: the hub unloads the current one, rewrites `model_path` through `/api/settings` and reloads |
| Voice Studio | the Qwen3-TTS models from `/api/catalog`, plus the tokenizer | model: `/api/models/load` after an orchestrator claim (downloads first if needed) |
| Video Studio | the model packs from `/api/models` (per engine, with per-file download state) | pack: becomes that engine's variant and the engine becomes the default |
| Music Studio | `model` - folders in `models/` (YuE2-3B, the VAEs, SheetSage2, MERT) | the stage-1 song model, through the studio's `/api/models/select` |
| Forge | `checkpoint`, `lora` (recursive), `vae`, `controlnet`, `upscaler` - Forge's `models/*` folders | checkpoint: `sd_model_checkpoint` through `/sdapi/v1/options` |

"Use" always goes through the GPU orchestrator first, so switching a model frees the card the
same way Generate does.

## Adding a model

**Add…** on any folder/file kind opens three sources:

* **HuggingFace** - a repository id (`owner/repo`). *Look up* shows the size, the file count and
  whether it is gated. Folder kinds download the whole repository (`snapshot_download`, resumable,
  into a folder named as you like); file kinds offer the single weight files in the repository
  (`.safetensors`, `.ckpt`, `.gguf`, `.pt`, `.pth`, `.bin`, `.onnx`).
* **Civitai** - search (filtered to the kind's type: Checkpoint, LORA, VAE, Controlnet; Image Studio's
  LoRAs are filtered to the Qwen base model). The latest version's primary file is downloaded and
  checked against the SHA-256 Civitai publishes.
* **Direct link** - any URL; an optional SHA-256 is verified after the download.

Direct and Civitai downloads write `<name>.part` and resume with a `Range` request if interrupted
or cancelled; HuggingFace downloads resume through the HuggingFace cache. Downloads run inside the
hub (two at a time), survive leaving the page, show in the **Downloads** panel with speed and ETA,
and finish with a toast. The activity feed logs the outcome.

The catalogue entries (the models a studio was built around) have a **Download** button when
missing and **Verify** / **Redownload** when installed: *Verify* checks every file of a HuggingFace
folder against the repository (presence and size; documentation and demo assets are ignored) and
single files against their known checksum; *Redownload* fetches again and replaces missing or
wrong files.

Tokens: **Settings → Models** holds a HuggingFace token (gated repositories) and a Civitai API key
(some downloads require one). They are stored in the hub's `data/settings.json` on this PC only.

## Paths

Studios on Linux run in containers and report paths as they see them (`/app/...`, `/cache/...`);
the hub maps those back to the host (`/app` → the studio folder, `/cache` → `$AI_CACHE`, default
`~/.cache/ai-tools`) so the page always shows real folders. Downloads go to the host folder; the
container sees them immediately through its bind mount.

## API

| | |
|---|---|
| `GET /api/models` | the whole page: per studio, kinds with installed items and catalogue, active model, tokens present; plus live downloads |
| `POST /api/models/download` | `{tool, kind, name, source}` - `source.type` = `hf` (`repo`, optional `allow_patterns`), `hf_file` (`repo`, `path`), `url` / `civitai` (`url`, optional `sha256`, `size`). Delegated studios: `{tool, variant_id}` (Video) or `{tool, name}` (Voice) |
| `GET /api/models/downloads`, `POST …/downloads/{id}/cancel`, `POST …/downloads/clear` | the queue |
| `POST /api/models/delete` | `{tool, kind, name}` (delegated: `{tool, name}`) |
| `POST /api/models/verify` | `{tool, kind, name}` → `{ok, message, missing, wrong_size}` |
| `POST /api/models/use` | `{tool, kind, name}` → `{ok, message}` |
| `GET /api/models/hf?repo=` | repository info for the Add dialog |
| `GET /api/models/civitai?q=&types=&base=` | search results (cached 5 min) |

Progress arrives on `/api/events` as `download` events (`id, tool, kind, name, status, bytes,
total, percent, speed, eta, message`); `status` is `queued`, `running`, `verifying`, `done`,
`error` or `cancelled`.

## Adding a new model to the catalogue

Edit the studio's `model_kinds` in `hub/tools.py`: a catalogue entry is `{"name", "label",
"detail", "size_h", "source"}` with the same `source` shape as the download API. A kind is
`{"id", "label", "dir", "layout": "folder" | "file", "exts", "marker", "recursive", "use",
"sources", "civitai": {"types", "base"}, "catalog"}`. Models that need code changes in the studio
(a different architecture) still need that code change - the page only manages files and the
studio's own selection API.
