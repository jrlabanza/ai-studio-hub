# GPU vendors: NVIDIA and AMD

Every studio and the hub detect the graphics card once, install the matching PyTorch build and switch
off the features that only exist for the other vendor. The user does nothing: the initialiser prints
what it found and what it chose.

## Detection (the contract)

Each studio's initialiser (Windows `Initialize.bat` / `setup.ps1` / `initialize.py`, Linux
`linux/initialize.sh`) runs the detection and writes **`.gpu.json`** in the studio's root folder:

```json
{ "vendor": "amd", "name": "AMD Radeon RX 9070 XT", "gfx": "gfx1201", "vram_mb": 16384,
  "backend": "rocm", "torch": "2.12.0+rocm7.14.1", "platform": "windows", "detected": "2026-10-01T12:00:00" }
```

| field | values |
|---|---|
| `vendor` | `nvidia`, `amd`, `cpu` |
| `backend` | `cuda`, `rocm`, `cpu` |
| `gfx` | AMD only: the LLVM target (`gfx1201`, `gfx1100`, `gfx1030`, …), `""` when unknown |
| `torch` | the build the initialiser installed (as `torch.__version__` reports it) |

Launchers (`webui-user.bat`, `Run.bat`, `start.bat`, `linux/entrypoint.sh`) and the apps read this file;
when it is missing they fall back to runtime detection (`torch.version.hip` / `torch.version.cuda`).

Detection order:

1. **NVIDIA** - `nvidia-smi` answers: vendor `nvidia`, backend `cuda`. The CUDA tag follows the driver
   as today (driver ≥ 580 → cu130, ≥ 528 → cu126).
2. **AMD** - no NVIDIA, but an AMD/Radeon display adapter exists (Windows: `Get-CimInstance
   Win32_VideoController`; Linux: `rocm-smi`/`amd-smi`, else `/sys/class/drm/card*/device/vendor` =
   `0x1002`): vendor `amd`, backend `rocm`. The gfx target comes from the adapter name
   (`docs/gpu-detect.ps1`, table below); unknown names use `device-all`.
3. **CPU** - neither: vendor `cpu`. Install the CPU torch build and warn that generation will be slow.

A `--gpu nvidia|amd|cpu` (or `-Gpu`) switch on every initialiser overrides the detection, and
`--gfx gfx1100` overrides the target.

## PyTorch builds

| vendor / platform | install |
|---|---|
| NVIDIA, Windows + Linux | `pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/<cu130\|cu126>` (unchanged) |
| AMD, **Windows** (native ROCm, public preview) | `pip install --index-url https://repo.amd.com/rocm/whl-multi-arch/ "torch[device-<gfx>]==2.12.0+rocm7.14.1" "torchvision[device-<gfx>]==0.27.0+rocm7.14.1" "torchaudio==2.11.0+rocm7.14.1"` - Python 3.11-3.14, AMD Software Adrenalin 26.x. `device-all` works for any supported card (bigger download). |
| AMD, Linux | `pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/rocm7.1` inside a `rocm/dev-ubuntu-24.04` based image; the container gets `/dev/kfd` and `/dev/dri` and the `video`/`render` groups instead of the NVIDIA runtime (`linux/compose.rocm.yml`). |
| CPU | `--index-url https://download.pytorch.org/whl/cpu` |

Pinned torch versions per studio stay as they are for NVIDIA; for AMD on Windows the torch version is
whatever `rocm7.14.1` ships (2.12.0 at the time of writing) because AMD builds one torch per ROCm
release - the initialiser installs the studio's other requirements *after* torch with
`--no-deps`-safe constraints so pip does not replace it with a CPU build.

### Windows AMD: name → gfx

| adapter name contains | gfx | notes |
|---|---|---|
| RX 9070, RX 9070 XT, AI PRO R9700 | `gfx1201` | RDNA 4, FP8 capable |
| RX 9060, RX 9060 XT | `gfx1200` | RDNA 4 |
| RX 7900 (XTX, XT, GRE), PRO W7900, W7800 | `gfx1100` | RDNA 3 |
| RX 7800 XT, RX 7700 (XT), W7700 | `gfx1101` | |
| RX 7600 (XT), RX 7650 | `gfx1102` | |
| Radeon 780M, 760M (Ryzen 7040/8040 iGPU) | `gfx1103` | APU, shares system RAM |
| Radeon 880M, 890M (Ryzen AI 300) | `gfx1150` | |
| Radeon 8060S / 8050S (Ryzen AI MAX) | `gfx1151` | |
| RX 6950/6900/6800 (XT), W6800 | `gfx1030` | RDNA 2; in the multi-arch wheel, not in AMD's official Windows matrix |
| RX 6700/6750 | `gfx1031` → use `device-all` | |
| RX 6600/6650/6500/6400, RX 5000, Vega | not in the wheel | CPU build + a clear message |

## What changes per studio on AMD

Common: `torch.cuda.*` keeps working (ROCm presents itself as the `cuda` device), so the apps' device
code is untouched. These are the CUDA-only extras and their AMD behaviour:

| studio | CUDA-only today | on AMD |
|---|---|---|
| Image Studio | bitsandbytes NF4 profiles (text encoder / transformer), nvidia-smi stats | pick the `bf16` + model-offload profile automatically (needs ≥ 16 GB; on 8-12 GB cards use sequential offload: slower but runs); stats from `torch.cuda.mem_get_info`; `fp8` transformer storage only on RDNA 4 |
| Voice Studio | none in the app; Chatterbox helper | same wheels for the Chatterbox venv; `attn_implementation="sdpa"` |
| Video Studio | nvidia-smi in `hardware.py`; engine flags `--fast fp16_accumulation`; model packs | engine started with `--disable-pinned-memory` (no `--fast`), stats from `torch.cuda`; INT8/GGUF packs load on ROCm, FP8 packs only on RDNA 4 |
| Music Studio | CUDA-graph decoder, `fp8` quantisation | `--quantization none`, eager decoder; the 8 GB "low VRAM" swap through RAM works unchanged |
| Forge | `--cuda-malloc`, `--cuda-stream`, `--pin-shared-memory`, SageAttention wheel, xformers | none of those flags; `--attention-pytorch`; no SageAttention; Forge's own `torch.version.hip` branch handles the rest |

Not available on Windows ROCm at all: Triton, xformers, flash-attn, SageAttention, bitsandbytes,
CUDA graphs. The initialisers skip those wheels with a one-line note.

## Hub

`hub/gpu.py` reads the card through whichever tool exists: `nvidia-smi`, else `rocm-smi`/`amd-smi`
(Linux), else Windows performance counters (`GPU Adapter Memory` / `GPU Process Memory`, any vendor).
The policies (one model at a time under 20 GB, share above) are the same on both vendors; the
check-up page says which vendor and backend every studio was set up for.

## Status

Implemented for every studio and the hub; verified end to end on NVIDIA (this machine). The AMD path
was built against AMD's published Windows wheels and matrices and is **awaiting a run on AMD
hardware** - the READMEs say so until that run is done.
