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

Launchers (`webui-user.bat`, `Run.bat`, `start.bat`, `linux/entrypoint.sh`) and the apps read this file (batch
files read a sibling `.gpu.cmd` with `set "AI_GPU_VENDOR=…"` / `AI_GPU_BACKEND` / `AI_GPU_GFX` lines instead);
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
| AMD, Linux | a second container image per studio, `ai/<tool>:rocm`, built from `linux/Dockerfile.rocm` and run with `linux/compose.rocm.yml` - see *Linux containers on AMD* below. |
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

## Linux containers on AMD (ROCm)

Every studio keeps its CUDA image and gains a ROCm one. Same layout, same bind mounts, same ports and
`ai.tool` label, so the launcher, the hub and `ai run` treat both alike; `.gpu.json` (`backend: rocm`,
`platform: linux`) decides which one the scripts use.

| | CUDA image (unchanged) | ROCm image |
|---|---|---|
| file | `linux/Dockerfile` + `linux/compose.yml` | `linux/Dockerfile.rocm` + `linux/compose.rocm.yml` |
| base | `nvidia/cuda:13.0.3-cudnn-devel-ubuntu24.04` | `rocm/dev-ubuntu-24.04:7.1.1` (brings `rocminfo` / `rocm-smi`) |
| image tag | `ai/<tool>:latest` | `ai/<tool>:rocm` |
| GPU access | `runtime: nvidia`, `NVIDIA_*` env | `devices: [/dev/kfd, /dev/dri]`, `group_add: ["${AI_VIDEO_GID}", "${AI_RENDER_GID}"]`, `security_opt: [seccomp=unconfined]`, `HSA_OVERRIDE_GFX_VERSION` passed through from `linux/.env` |
| torch | the studio's CUDA pin | the official PyTorch ROCm wheel closest to that pin (table below), from `https://download.pytorch.org/whl/<rocm tag>` |

PyTorch ROCm wheels available (October 2026): `rocm7.1` → torch 2.11.0 and 2.13.0 (py3.10–3.14), `rocm7.0` → 2.10.0,
`rocm6.4` → 2.9.1, `rocm7.2` → 2.14.1. Chosen per studio (as built and verified here):

| studio | CUDA pin | ROCm image installs |
|---|---|---|
| Image Studio | torch 2.13.0 / py3.12 | `torch==2.13.0 torchvision==0.28.0` from `rocm7.1` |
| Voice Studio | torch 2.11.0 / py3.11 | `torch==2.11.0 torchaudio==2.11.0` from `rocm7.1` (exact pin); the Chatterbox helper, whose package pins torch 2.6, runs on `torch==2.9.1` from `rocm6.4` with its own pins kept |
| Video Studio | torch 2.13.0 / py3.12 | `torch==2.13.0 torchvision==0.28.0 torchaudio==2.11.0` from `rocm7.1` (the index carries torchaudio 2.10/2.11 only, same as the CUDA lock) |
| Music Studio | torch 2.10.0 / py3.12 | `torch==2.10.0` from `rocm7.0` |
| Forge | torch 2.10.0 / py3.13 | `torch==2.10.0 torchvision==0.25.0` from `rocm7.0` |

The Dockerfile keeps everything else identical to the CUDA one (Python version from deadsnakes, the same
`constraints.txt`, the same entrypoint); it drops the CUDA-only wheels (SageAttention, flash-attn,
bitsandbytes, triton-windows, nvidia-* packages, `TORCH_CUDA_ARCH_LIST`) and the `nvcc`-dependent steps.
Workarounds for cards outside AMD's Linux matrix: `linux/.env` may set `HSA_OVERRIDE_GFX_VERSION`
(`10.3.0` for RX 6700/6600 gfx103x, `11.0.0` for gfx1103 iGPUs); the initialiser writes it when it knows the
chip.

`linux/initialize.sh` on an AMD box: checks `/dev/kfd` + `/dev/dri` (the amdgpu driver is in the Ubuntu
kernel; no ROCm host install is needed), installs Docker without the NVIDIA toolkit, adds the user to the
`video` and `render` groups, builds `ai/<tool>:rocm`, reads the gfx target with `rocminfo` inside the
container, writes `.gpu.json`, runs the boot test. `run.sh` / `stop.sh` / `test.sh` pick the compose file
from `.gpu.json` (or `AI_GPU=rocm|cuda` to force). The `ai` launcher and the hub do the same.

## What changes per studio on AMD

Common: `torch.cuda.*` keeps working (ROCm presents itself as the `cuda` device), so the apps' device
code is untouched. These are the CUDA-only extras and their AMD behaviour:

| studio | CUDA-only today | on AMD |
|---|---|---|
| Image Studio | bitsandbytes NF4 profiles (text encoder / transformer), nvidia-smi stats | pick the `bf16` + model-offload profile automatically (needs ≥ 16 GB; on 8-12 GB cards use sequential offload: slower but runs); stats from `torch.cuda.mem_get_info`; `fp8` transformer storage only on RDNA 4 |
| Voice Studio | none in the app; Chatterbox helper | same wheels for the Chatterbox venv; `attn_implementation="sdpa"` |
| Video Studio | nvidia-smi in `hardware.py`; engine flags `--fast fp16_accumulation`; model packs | engine started with `--disable-pinned-memory` (no `--fast`), stats from `torch.cuda`; INT8/GGUF packs load on ROCm, FP8 packs only on RDNA 4 |
| Music Studio | CUDA-graph decoder, `fp8` quantisation | `--quantization none`, eager decoder; the 8 GB "low VRAM" swap through RAM works unchanged |
| Forge | `--cuda-malloc`, `--cuda-stream`, `--pin-shared-memory`, SageAttention wheel, xformers | none of those flags; `--use-pytorch-cross-attention` (Forge's SDPA switch); no SageAttention; Forge's own `torch.version.hip` branch handles the rest |

Not available on Windows ROCm at all: Triton, xformers, flash-attn, SageAttention, bitsandbytes,
CUDA graphs. The initialisers skip those wheels with a one-line note.

## Hub

`hub/gpu.py` reads the card through whichever tool exists: `nvidia-smi`, else `rocm-smi`/`amd-smi`
(Linux), else Windows performance counters (`GPU Adapter Memory` / `GPU Process Memory`, any vendor). On
Linux the hub starts a studio's ROCm container (`compose.rocm.yml`, image `ai/<tool>:rocm`) when its
`.gpu.json` says `rocm`.
The policies (one model at a time under 20 GB, share above) are the same on both vendors; the
check-up page says which vendor and backend every studio was set up for.

## Status

Implemented for every studio and the hub on both platforms; verified end to end on NVIDIA (this machine).
The AMD paths - Windows (AMD's native wheels) and Linux (the ROCm containers, which build and boot here
without a GPU) - are **awaiting a run on AMD hardware**; the READMEs say so until that run is done.
