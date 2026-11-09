#!/usr/bin/env bash
# AI Studio Hub - install.sh: from a bare Linux box to the hub running, in one command.
#
#   curl -fsSL https://raw.githubusercontent.com/jrlabanza/ai-studio-hub/main/install.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/jrlabanza/ai-studio-hub/main/install.sh | bash -s -- --skip forge
#   ./install.sh                   from a clone: sets up that checkout
#
#   --dir DIR        where the hub lives (default: this checkout, else ~/AI/ai-studio-hub)
#   --skip TOOL      leave a studio out: image, voice, video, music, forge (repeat, or comma-separate)
#   --no-start       set everything up, but do not start the hub
#   --no-models      environments only: no studio downloads its models (Video Studio's pack included)
#   --no-browser     do not open the browser at the end
#   --no-desktop     no app-menu entry
#   --autostart      start the hub when you log in (a systemd user service; undo: linux/install-autostart.sh --remove)
#   --gpu-unlock     NVIDIA RTX 20/30/40 only, EXPERIMENTAL: also install gpu-unlock (faster BF16), needs a reboot.
#                    On an RTX 3070 Laptop it made every shutdown kernel-panic; Video Studio's H3 path is INT8 anyway.
#   --dry-run        print every step it would take and change nothing
#
# Steps: 1 checks and graphics card  2 system packages (+ NVIDIA driver)  3 the code
#        4 Docker (+ NVIDIA Container Toolkit)  5 the studios, each with its own linux/initialize.sh
#        6 the hub (linux/initialize.sh)  7 start the hub + Video Studio's model pack  [8 gpu-unlock]
# Safe to re-run: every step checks first and skips what is done, and so do the studios'
# initializers. Studio containers that are running are never restarted (their boot tests are skipped).
#
# Everything sits inside main(), so "curl | bash" reads the whole script before anything runs.

main() {
set -uo pipefail
REPO_URL="https://github.com/jrlabanza/ai-studio-hub.git"
RAW_URL="https://raw.githubusercontent.com/jrlabanza/ai-studio-hub/main"
TOOL_IDS=(image tts video music forge)
DIR=""; DRY=0; START=1; MODELS=1; BROWSER=1; DESKTOP=1; UNLOCK=0; AUTOSTART=0
declare -A SKIP=()

usage(){ sed -n '2,21p' "${BASH_SOURCE[0]:-/dev/null}" 2>/dev/null | sed 's/^# \?//' | grep . \
  || echo "usage: install.sh [--dir DIR] [--skip TOOL] [--no-start] [--no-models] [--no-browser] [--no-desktop] [--autostart] [--gpu-unlock] [--dry-run]"; }
add_skip(){ # accepts the ids and the studio names people use
  local t
  for t in ${1//,/ }; do
    case ${t,,} in
      image|images) SKIP[image]=1 ;; tts|voice|qwen-tts) SKIP[tts]=1 ;; video|lumen) SKIP[video]=1 ;;
      music|yue2|yue) SKIP[music]=1 ;; forge) SKIP[forge]=1 ;;
      *) echo "unknown studio for --skip: $t (image, voice, video, music, forge)" >&2; exit 1 ;;
    esac
  done
}
while (( $# )); do
  case $1 in
    --dir) DIR="${2:-}"; shift ;; --dir=*) DIR="${1#*=}" ;;
    --skip) add_skip "${2:-}"; shift ;; --skip=*) add_skip "${1#*=}" ;;
    --no-start) START=0 ;; --no-models) MODELS=0 ;; --no-browser) BROWSER=0 ;; --no-desktop) DESKTOP=0 ;;
    --gpu-unlock) UNLOCK=1 ;; --autostart) AUTOSTART=1 ;; --dry-run|-n) DRY=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1 (--help lists them)" >&2; exit 1 ;;
  esac; shift
done

say(){ printf '\n\033[1m[%s] %s\033[0m\n' "$1" "$2"; }
ok(){ printf '      %s\n' "$*"; }
note(){ printf '      \033[33m%s\033[0m\n' "$*"; }
bad(){ printf '      \033[31m%s\033[0m\n' "$*"; }
die(){ bad "$*"; echo; echo "  Setup did not complete. Fix the error above and run the installer again - finished steps are skipped."; echo; exit 1; }
quote(){ local out="" a; for a in "$@"; do out+="$(printf '%q' "$a") "; done; printf '%s' "${out% }"; }
# run CMD...: does it, or in --dry-run only says what it would do
run(){ if (( DRY )); then printf '      \033[36mwould run:\033[0m %s\n' "$(quote "$@")"; return 0; fi; "$@"; }

# sudo: asked for once, then kept fresh while the long steps (image builds) run
SUDO_KEEP=""
need_sudo(){
  (( DRY )) && return 0
  [[ -n $SUDO_KEEP ]] && return 0
  echo "      (your password is needed for system changes)"
  # shellcheck disable=SC2024  # the redirect is for the password prompt (curl | bash has no stdin)
  sudo -v </dev/tty 2>/dev/null || sudo -v || die "sudo is needed for this step"
  ( while kill -0 "$$" 2>/dev/null; do sudo -n true 2>/dev/null; sleep 50; done ) &
  SUDO_KEEP=$!
}
trap '[[ -n $SUDO_KEEP ]] && kill "$SUDO_KEEP" 2>/dev/null' EXIT

echo; echo "  AI Studio Hub - setup"; echo "  ---------------------"
(( DRY )) && echo "  (dry run: nothing is changed, every step is only printed)"

# --- 1. Checks and graphics card ---------------------------------------------
say 1/7 "Checks and graphics card"
[[ $(uname -s) == Linux ]] || die "this installer is for Linux; on Windows use install.ps1 (README: Quick start)"
(( EUID )) || die "run this as your normal user, not as root or with sudo - it asks for your password when it needs it"
OS_NAME="Linux"
# shellcheck disable=SC1091
[[ -r /etc/os-release ]] && OS_NAME=$(. /etc/os-release; echo "${PRETTY_NAME:-Linux}")
APT=0; command -v apt-get >/dev/null && APT=1
ok "system: $OS_NAME$( (( APT )) || echo ' (no apt: install git, python3-venv and Docker yourself if they are missing)')"
RAM_MB=$(awk '/^MemTotal:/ {print int($2 / 1024)}' /proc/meminfo 2>/dev/null); RAM_MB=${RAM_MB:-0}
ok "memory: $(( (RAM_MB + 512) / 1024 )) GB"

# NVIDIA through nvidia-smi; otherwise what the PCI bus has (display controllers only).
GPU_VENDOR=none; GPU_NAME=""; GPU_VRAM=0; GPU_CC=""; GPU_DRIVER=""; GPU_BUS=""
NV_PCI=0; AMD_PCI=0; AMD_NAME=""
for v in /sys/bus/pci/devices/*/vendor; do
  [[ -r $v ]] || continue
  d="${v%/vendor}"; [[ $(cat "$d/class" 2>/dev/null) == 0x03* ]] || continue
  case $(cat "$v") in
    0x10de) NV_PCI=1 ;;
    0x1002) AMD_PCI=1; [[ -n $AMD_NAME ]] || AMD_NAME=$(cat "$d"/drm/card*/device/product_name 2>/dev/null | head -1) ;;
  esac
done
if command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1; then
  GPU_VENDOR=nvidia
  IFS=, read -r GPU_NAME GPU_VRAM GPU_DRIVER GPU_BUS < <(nvidia-smi --query-gpu=name,memory.total,driver_version,pci.bus_id --format=csv,noheader,nounits | head -1)
  GPU_CC=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | tr -d ' ')
  [[ $GPU_CC =~ ^[0-9]+\.[0-9]+$ ]] || GPU_CC=""
  GPU_NAME=$(echo "$GPU_NAME" | xargs); GPU_VRAM=$(echo "$GPU_VRAM" | tr -dc '0-9'); GPU_DRIVER=$(echo "$GPU_DRIVER" | xargs); GPU_BUS=$(echo "$GPU_BUS" | xargs)
  ok "graphics card: $GPU_NAME, $(( (${GPU_VRAM:-0} + 512) / 1024 )) GB VRAM${GPU_CC:+, compute capability $GPU_CC}, driver $GPU_DRIVER -> CUDA containers"
elif (( NV_PCI )); then
  GPU_VENDOR=nvidia-nodriver
  note "graphics card: NVIDIA, but its driver is not working yet (nvidia-smi does not answer) - step 2 installs it"
elif (( AMD_PCI )); then
  GPU_VENDOR=amd; GPU_NAME="${AMD_NAME:-AMD Radeon}"
  ok "graphics card: $GPU_NAME (AMD) -> ROCm containers; the amdgpu driver in the kernel is all the host needs"
else
  note "graphics card: no NVIDIA or AMD card found. The studios need one; the installer carries on, but they will not start."
fi

# --- 2. System packages ------------------------------------------------------
say 2/7 "System packages"
pkgs=()
command -v git >/dev/null || pkgs+=(git)
command -v curl >/dev/null || pkgs+=(curl)
[[ -d /etc/ssl/certs ]] || pkgs+=(ca-certificates)
PY=""
for c in python3.13 python3.12 python3.11 python3.14 python3; do
  command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null && { PY=$c; break; }
done
[[ -n $PY ]] || pkgs+=(python3)
if [[ -z $PY ]] || ! "$PY" -c 'import ensurepip, venv' 2>/dev/null; then pkgs+=(python3-venv python3-pip); fi
if (( ${#pkgs[@]} )); then
  (( APT )) || die "missing: ${pkgs[*]} - install them with your package manager and run this again"
  ok "installing: ${pkgs[*]}"
  need_sudo
  run sudo apt-get update -qq || die "apt-get update failed"
  run sudo apt-get install -y -q "${pkgs[@]}" || die "could not install ${pkgs[*]}"
  [[ -n $PY ]] || PY=python3
else
  ok "git, curl, $PY with venv: present"
fi
REBOOT_FOR_DRIVER=0
if [[ $GPU_VENDOR == nvidia-nodriver ]]; then
  command -v ubuntu-drivers >/dev/null || (( DRY )) || die "install the NVIDIA driver for your card (nvidia-smi must work), reboot, and run this again"
  ok "installing the recommended NVIDIA driver (ubuntu-drivers)"
  need_sudo
  run sudo ubuntu-drivers install || die "the NVIDIA driver install failed (sudo ubuntu-drivers install)"
  REBOOT_FOR_DRIVER=1
  if (( ! DRY )); then
    echo; echo "  The NVIDIA driver is installed. Reboot, then run the same command again - it continues from here."; echo
    exit 0
  fi
  note "(after this the installer stops and asks for a reboot; the dry run carries on)"
fi

# --- 3. The code -------------------------------------------------------------
say 3/7 "The code"
SELF_DIR=""
if [[ -n ${BASH_SOURCE[0]:-} && -f ${BASH_SOURCE[0]} ]]; then SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; fi
is_hub(){ [[ -f $1/hub/__init__.py && -f $1/linux/initialize.sh ]]; }
if [[ -n $DIR ]]; then ROOT=$(realpath -m "${DIR/#\~/$HOME}")
elif [[ -n $SELF_DIR ]] && is_hub "$SELF_DIR"; then ROOT=$SELF_DIR
else ROOT="$HOME/AI/ai-studio-hub"; fi
export GIT_TERMINAL_PROMPT=0     # a repository you cannot access fails at once instead of asking for a password
if is_hub "$ROOT"; then
  ok "using the checkout in $ROOT (left as it is - 'git pull' there to update)"
elif [[ -e $ROOT && -n $(ls -A "$ROOT" 2>/dev/null) ]]; then
  die "$ROOT exists and is not an AI Studio Hub checkout - pick another folder with --dir"
else
  ok "cloning $REPO_URL into $ROOT"
  run mkdir -p "$(dirname "$ROOT")"
  run git clone "$REPO_URL" "$ROOT" || die "git clone failed - check your internet connection"
fi

# Which studios are where: the hub's own lookup (hub/config.py tool_dir - inside the hub, the
# submodule layout, or next to it, the ~/AI layout) - the same answer the hub will use.
declare -A T_NAME=() T_SUB=() T_DIR=() T_CO=() T_MOD=()
scan(){
  local id name sub dir co mod
  T_NAME=([image]="Image Studio" [tts]="Voice Studio" [video]="Video Studio" [music]="Music Studio" [forge]="Forge Studio")
  T_SUB=([image]="Image gen" [tts]="qwen tts" [video]="video-gen" [music]="Yue2" [forge]="forge")
  is_hub "$ROOT" || return 0
  while IFS=$'\t' read -r id name sub dir co mod; do
    [[ -n $id ]] || continue
    T_NAME[$id]=$name; T_SUB[$id]=$sub; T_DIR[$id]=$dir; T_CO[$id]=$co; T_MOD[$id]=$mod
  done < <("$PY" -I - "$ROOT" 2>/dev/null <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from hub.config import DEFAULT_TOOLS, tool_dir, tool_dir_candidates
from hub.tools import TOOLS
for t in TOOLS.values():
    d = tool_dir(t.id)
    co = t.is_checkout(d)
    if not co:
        for c in tool_dir_candidates(t.id):
            if t.is_checkout(c):
                d, co = c, True
                break
    mod = co and t.model_present(d)[0]
    print("\t".join([t.id, t.name, DEFAULT_TOOLS[t.id]["dir"], str(d), str(int(co)), str(int(bool(mod)))]))
PY
)
}
scan
declare -A GONE=()   # studios that could not be fetched
for id in "${TOOL_IDS[@]}"; do
  [[ -n ${SKIP[$id]:-} ]] && { ok "${T_NAME[$id]}: skipped (--skip)"; continue; }
  if [[ ${T_CO[$id]:-0} == 1 ]]; then ok "${T_NAME[$id]}: ${T_DIR[$id]}"; continue; fi
  ok "${T_NAME[$id]}: fetching the '${T_SUB[$id]}' submodule"
  if ! run git -C "$ROOT" submodule update --init --recursive -- "${T_SUB[$id]}"; then
    note "could not fetch ${T_NAME[$id]} (a private repository you have no access to, or no internet) - the hub runs without it"
    GONE[$id]=1
  fi
done
(( DRY )) || scan
studios=()   # the ones to set up
for id in "${TOOL_IDS[@]}"; do
  [[ -z ${SKIP[$id]:-} && -z ${GONE[$id]:-} ]] || continue
  if (( ! DRY )) && [[ ${T_CO[$id]:-0} != 1 ]]; then note "${T_NAME[$id]}: no checkout found after fetching - left out"; GONE[$id]=1; continue; fi
  [[ -n ${T_DIR[$id]:-} ]] || T_DIR[$id]="$ROOT/${T_SUB[$id]}"
  studios+=("$id")
done

# --- 4. Docker ---------------------------------------------------------------
if [[ $GPU_VENDOR == amd ]]; then say 4/7 "Docker"; else say 4/7 "Docker + NVIDIA Container Toolkit"; fi
DK=(docker)
docker_reach(){ # sets DK to what reaches the daemon: docker, or sudo -n docker until the docker group is active
  if docker info >/dev/null 2>&1; then DK=(docker); return 0; fi
  if sudo -n docker info >/dev/null 2>&1; then DK=(sudo -n docker); return 0; fi
  return 1
}
INSTALLER=""   # the studios' own Docker installer (identical idea in every studio's linux/)
for id in "${studios[@]}"; do [[ -f ${T_DIR[$id]}/linux/install-docker.sh ]] && { INSTALLER="${T_DIR[$id]}/linux/install-docker.sh"; break; }; done
[[ -n $INSTALLER || ${#studios[@]} -eq 0 ]] || INSTALLER="${T_DIR[${studios[0]}]}/linux/install-docker.sh"
DOCKER_FRESH=0
want_nvidia=0; [[ $GPU_VENDOR == nvidia* ]] && want_nvidia=1
if ! command -v docker >/dev/null; then
  ok "not installed - installing Docker Engine$( (( want_nvidia )) && echo ' + the NVIDIA Container Toolkit') with $(basename "$(dirname "$(dirname "${INSTALLER:-?/x/y}")")")'s linux/install-docker.sh"
  [[ -n $INSTALLER ]] || (( DRY )) || die "no studio checkout to take the Docker installer from"
  need_sudo
  if (( want_nvidia )); then run sudo bash "$INSTALLER" || die "Docker install failed"
  else run sudo bash "$INSTALLER" --no-nvidia || die "Docker install failed"; fi
  DOCKER_FRESH=1
else
  ok "$(docker --version 2>/dev/null)"
  if ! docker_reach; then
    # installed, but this login is not in the docker group yet: sudo bridges the gap
    need_sudo
    (( DRY )) || docker_reach || die "cannot reach the Docker daemon (is it running? sudo systemctl start docker)"
  fi
  if (( want_nvidia )) && ! "${DK[@]}" info 2>/dev/null | grep -q 'Runtimes:.*nvidia'; then
    note "the NVIDIA runtime is not registered in Docker - adding the NVIDIA Container Toolkit (this restarts Docker)"
    [[ -f $INSTALLER ]] || (( DRY )) || die "no studio checkout to take the installer from - install nvidia-container-toolkit, run: sudo nvidia-ctk runtime configure --runtime=docker"
    need_sudo
    run sudo bash "$INSTALLER" || die "NVIDIA Container Toolkit install failed"
  elif (( want_nvidia )); then ok "NVIDIA runtime registered"; fi
  if ! "${DK[@]}" compose version >/dev/null 2>&1; then
    note "the Docker compose plugin is missing - installing it"
    need_sudo
    run sudo apt-get install -y -q docker-compose-plugin || run sudo apt-get install -y -q docker-compose-v2 \
      || die "install the Docker compose plugin (docker-compose-plugin or docker-compose-v2) and run this again"
  else ok "compose $("${DK[@]}" compose version --short 2>/dev/null)"; fi
fi
if (( DOCKER_FRESH && ! DRY )); then docker_reach || die "Docker was installed but its daemon does not answer"; fi
[[ ${DK[0]} == sudo ]] && note "your docker group membership applies after you log out and back in; until then sudo is used"

# --- 5. The studios ----------------------------------------------------------
say 5/7 "The studios (each with its own linux/initialize.sh: image, models, boot test)"
TEST_ARG=()
running=$("${DK[@]}" ps --filter label=ai.tool --format '{{.Names}}' 2>/dev/null | tr '\n' ' ')
if [[ -n ${running// /} ]]; then
  note "running studio containers: ${running% } - boot tests are skipped so nothing running is restarted"
  TEST_ARG=(--no-test)
fi
STUDIO_ARGS=(--no-desktop)            # the hub is the one app-menu entry
(( MODELS )) || STUDIO_ARGS+=(--no-models)
FAILED=()
for id in "${studios[@]}"; do
  init="${T_DIR[$id]}/linux/initialize.sh"
  echo; echo "  --- ${T_NAME[$id]}  ($init)"
  if (( ! DRY )) && [[ ! -f $init ]]; then bad "no linux/initialize.sh in ${T_DIR[$id]}"; FAILED+=("${T_NAME[$id]}"); continue; fi
  if ! run bash "$init" "${STUDIO_ARGS[@]}" "${TEST_ARG[@]}" </dev/null; then
    bad "${T_NAME[$id]} setup did not complete - the others carry on; re-run the installer (or $init) to retry"
    FAILED+=("${T_NAME[$id]}")
  fi
done
(( ${#studios[@]} )) || note "no studios to set up"

# --- 6. The hub --------------------------------------------------------------
say 6/7 "The hub (its .venv, a check of every studio, the app-menu entry)"
HUB_ARGS=(); (( DESKTOP )) || HUB_ARGS+=(--no-desktop)
run bash "$ROOT/linux/initialize.sh" "${HUB_ARGS[@]}" </dev/null || die "the hub's setup failed (linux/initialize.sh)"
(( AUTOSTART )) && { run bash "$ROOT/linux/install-autostart.sh" || note "autostart was not set up (linux/install-autostart.sh)"; }
(( DRY )) || scan

# --- 7. Start + Video Studio's model pack ------------------------------------
say 7/7 "Start"
HUB_PORT=$("$PY" -I -c 'import json, sys
try: print(int(json.load(open(sys.argv[1])).get("hub_port") or 7900))
except Exception: print(7900)' "$ROOT/data/settings.json" 2>/dev/null)
HUB_PORT=${HUB_PORT:-7900}; URL="http://localhost:$HUB_PORT"; API="http://127.0.0.1:$HUB_PORT"
jget(){ "$PY" -I -c 'import json, sys
d = json.load(sys.stdin)
for k in sys.argv[1].split("."):
    d = d.get(k, "") if isinstance(d, dict) else ""
print(d)' "$1" 2>/dev/null; }
hub_up(){ curl -sf -o /dev/null --max-time 3 "$API/api/state"; }

# What Video Studio will choose, from its own models_catalog.recommend_variant (the code the container
# runs - the repo is mounted at /app) fed with this machine's card and memory. Prints "id|label|GB".
pack_preview(){
  local vdir=${T_DIR[video]:-} backend=cuda gfx=""
  [[ -n $vdir && -f $vdir/backend/app/models_catalog.py ]] || return 0
  [[ $GPU_VENDOR == amd ]] && backend=rocm
  [[ -f $vdir/.gpu.json ]] && gfx=$("$PY" -I -c 'import json, sys; print(json.load(open(sys.argv[1])).get("gfx", ""))' "$vdir/.gpu.json" 2>/dev/null)
  "$PY" -I - "$vdir/backend" "${GPU_VRAM:-0}" "$RAM_MB" "$GPU_CC" "$backend" "$gfx" 2>/dev/null <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from app.models_catalog import FILES, VARIANTS, recommend_variant, variant_file_ids
v = recommend_variant("h3", int(sys.argv[2] or 0), int(sys.argv[3] or 0), sys.argv[4], sys.argv[5], sys.argv[6])
print(f"{v}|{VARIANTS[v].label}|{sum(FILES[f].size for f in variant_file_ids(v)) / 1e9:.1f}")
PY
}
video_models(){
  local vdir=${T_DIR[video]:-} prev rec label gb
  prev=$(pack_preview)
  IFS='|' read -r rec label gb <<< "$prev"
  if [[ -n ${SKIP[video]:-}${GONE[video]:-} ]]; then return 0; fi
  if (( ! MODELS )); then ok "Video Studio: no model pack requested (--no-models)${rec:+; it would pick $rec}"; return 0; fi
  if [[ ${T_MOD[video]:-0} == 1 ]]; then ok "Video Studio already has a model pack${rec:+ (for this card it recommends $rec)} - more in Video Studio > Models"; return 0; fi
  if [[ -f $vdir/data/settings.json ]] && grep -qE '"auto_download_models": *false' "$vdir/data/settings.json"; then
    note "Video Studio's automatic model download is turned off (shared model store) - pick a pack in Video Studio > Models"; return 0
  fi
  [[ -n $rec ]] && ok "H3 pack for this card: $rec - $label, $gb GB (Video Studio makes the final choice)"
  if (( ! START )); then ok "Video Studio downloads its recommended H3 pack the first time it starts"; return 0; fi
  if (( DRY )); then
    ok "would start Video Studio through the hub, ask it for its recommended H3 pack, and queue only that one:"
    run curl -sf -X POST "$API/api/tools/video/start"
    run curl -sf -X POST -H 'Content-Type: application/json' -d "{\"tool\":\"video\",\"variant_id\":\"${rec:-<recommended>}\"}" "$API/api/models/download"
    return 0
  fi
  ok "starting Video Studio to fetch its pack (its first start queues the recommended pack by itself) ..."
  curl -sf -o /dev/null -X POST "$API/api/tools/video/start" || { note "the hub did not accept the start - open Video Studio once and it downloads its pack"; return 0; }
  local t0 st="" port=""
  t0=$(date +%s)
  while (( $(date +%s) - t0 < 420 )); do
    st=$(curl -sf --max-time 5 "$API/api/tools/video" | jget state)
    [[ $st == running || $st == error ]] && break
    sleep 5
  done
  if [[ $st != running ]]; then note "Video Studio did not come up (${st:-no answer}) - see its log in the hub; it downloads its pack on its first start"; return 0; fi
  port=$(curl -sf --max-time 5 "$API/api/tools/video" | jget port); port=${port:-8765}
  local sys models gname gcc live
  sys=$(curl -sf --max-time 10 "http://127.0.0.1:$port/api/system")
  models=$(curl -sf --max-time 20 "http://127.0.0.1:$port/api/models")
  gname=$(jget system.gpu.name <<< "$sys"); gcc=$(jget system.gpu.compute_capability <<< "$sys")
  live=$(jget recommended.h3 <<< "$models")
  [[ -n $gname ]] && ok "Video Studio sees: $gname${gcc:+ (compute capability $gcc)}"
  if [[ -n $live ]]; then
    [[ $live != "$rec" ]] && ok "Video Studio chose: $live"
    rec=$live
  fi
  [[ -n $rec ]] || { note "could not read Video Studio's recommendation - open Video Studio > Models"; return 0; }
  if curl -sf -o /dev/null --max-time 60 -X POST -H 'Content-Type: application/json' \
       -d "{\"tool\":\"video\",\"variant_id\":\"$rec\"}" "$API/api/models/download"; then
    ok "downloading $rec in the background (only this pack; resumable) - progress in Video Studio > Models or the hub's Models page"
  else
    note "the download request was not accepted - start it from Video Studio > Models"
  fi
}

if (( ! START )); then
  ok "not started (--no-start). Start it with: $ROOT/linux/run.sh   (or \"AI – Studio Hub\" in the app menu)"
elif hub_up; then
  other=$(curl -sf --max-time 3 "$API/api/state" | jget app.root)
  if [[ -n $other && $(realpath -m "$other") != "$ROOT" ]]; then
    note "another AI Studio Hub ($other) is already running on port $HUB_PORT - stop it ($other/linux/stop.sh) and run this again to start this one"
    START=0
  else
    ok "AI Studio Hub is already running at $URL"
  fi
else
  run bash "$ROOT/linux/run.sh" --background --no-open </dev/null || die "the hub did not start (see $ROOT/data/logs/hub.log)"
  if (( ! DRY )); then
    for _ in $(seq 1 60); do hub_up && break; sleep 1; done
    hub_up || die "the hub did not answer on $URL within a minute (log: $ROOT/data/logs/hub.log)"
    ok "AI Studio Hub is running at $URL  (stop: $ROOT/linux/stop.sh)"
  fi
fi
echo; echo "  --- Video Studio model pack"
if [[ $GPU_VENDOR == nvidia ]]; then ok "card: $GPU_NAME${GPU_CC:+ (compute capability $GPU_CC)}, $(( (RAM_MB + 512) / 1024 )) GB RAM"
elif [[ $GPU_VENDOR == amd ]]; then ok "card: $GPU_NAME (ROCm), $(( (RAM_MB + 512) / 1024 )) GB RAM"; fi
video_models

# --- 8. gpu-unlock (only with --gpu-unlock) ----------------------------------
REBOOT_FOR_UNLOCK=0
gpu_unlock(){
  say 8 "gpu-unlock (BF16 tensor rate for GeForce RTX 20/30/40)"
  [[ $GPU_VENDOR == nvidia ]] || { note "skipped: it is for NVIDIA cards with a working driver"; return 0; }
  local major=${GPU_CC%%.*} minor=${GPU_CC#*.}
  [[ -n $GPU_CC ]] || { note "skipped: the driver did not report the compute capability"; return 0; }
  if (( major >= 10 )); then note "skipped: RTX 50 (Blackwell) runs NVFP4 natively - gpu-unlock is not for it"; return 0; fi
  if (( major * 10 + minor < 75 )) || [[ $GPU_NAME != *RTX* ]]; then note "skipped: only GeForce RTX 20/30/40 cards ($GPU_NAME is not one)"; return 0; fi
  (( APT )) || { note "skipped: this step knows the Debian/Ubuntu path only (see https://github.com/Zagitta/gpu-unlock)"; return 0; }
  # nvidia-smi says 00000000:01:00.0; the kernel parameter wants 0000:01:00.0
  local addr; addr=$(echo "$GPU_BUS" | tr 'A-F' 'a-f' | sed -E 's/^0*([0-9a-f]{4}):/\1:/')
  [[ $addr =~ ^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f]$ ]] || { note "skipped: could not read the card's PCI address ($GPU_BUS)"; return 0; }
  ok "card: $GPU_NAME at $addr"
  if command -v mokutil >/dev/null && mokutil --sb-state 2>/dev/null | grep -qi 'SecureBoot enabled'; then
    note "Secure Boot is on: the gpu-unlock module is unsigned and will not load unless you enrol a key (DKMS asks) or turn Secure Boot off"
  fi
  # Measured on an RTX 3070 Laptop (driver 595, kernel 7.0): the laptop settings below (runtime power management off)
  # made every power-off panic in nv_pci_shutdown, and H3 - which runs on the INT8 path - gained about 1%.
  note "experimental: on an RTX 3070 Laptop this made every shutdown end in a kernel panic, for ~1% on Video Studio"
  if ls /sys/class/power_supply/BAT* >/dev/null 2>&1; then
    note "this is a laptop: the shutdown panic above was seen on one - test a full power-off right after the reboot"
  fi
  if (( ! DRY )) && [[ -r /dev/tty ]]; then
    printf '      This builds a kernel module and changes the boot settings (a reboot applies it). Continue? [y/N] '
    local a=""; read -r a </dev/tty || a=""
    [[ ${a,,} == y* ]] || { ok "left alone"; return 0; }
  fi
  need_sudo
  local src="$HOME/gpu-unlock-src" conf=/etc/modprobe.d/gpu-unlock-nvidia.conf bak=/etc/default/grub.bak-gpu-unlock changed=0
  if dpkg -s gpu-unlock-dkms >/dev/null 2>&1; then
    ok "gpu-unlock packages already installed (to rebuild: make -C $src install)"
  else
    local p tools=()
    for p in git make dpkg-dev "linux-headers-$(uname -r)"; do dpkg -s "$p" >/dev/null 2>&1 || tools+=("$p"); done
    if (( ${#tools[@]} )); then run sudo apt-get install -y -q "${tools[@]}" || { bad "build tools could not be installed"; return 1; }; fi
    if [[ -d $src/.git ]]; then ok "source: $src (present)"; else run git clone https://github.com/Zagitta/gpu-unlock "$src" || { bad "clone failed"; return 1; }; fi
    ok "building and installing the packages (make install: gpu-unlock-dkms + gpu-unlock-firmware)"
    run make -C "$src" install </dev/null || { bad "make install failed - see above"; return 1; }
    changed=1
  fi
  # the kernel command line: back up /etc/default/grub once, then add (or correct) gpu_unlock.targets=
  if grep -qE "^GRUB_CMDLINE_LINUX_DEFAULT=.*gpu_unlock\.targets=$addr([\" ]|$)" /etc/default/grub; then
    ok "GRUB already has gpu_unlock.targets=$addr"
  else
    [[ -e $bak ]] && bak="$bak.$(date +%Y%m%d-%H%M%S)"
    run sudo cp -p /etc/default/grub "$bak"
    ok "backed up /etc/default/grub to $bak"
    run sudo sed -i -E "/^GRUB_CMDLINE_LINUX_DEFAULT=/{s/[[:space:]]*gpu_unlock\.targets=[^\" ]*//g;s/\"\$/ gpu_unlock.targets=$addr\"/;s/=\" /=\"/}" /etc/default/grub
    if (( ! DRY )) && ! grep -qE "^GRUB_CMDLINE_LINUX_DEFAULT=.*gpu_unlock\.targets=$addr" /etc/default/grub; then
      bad "could not edit GRUB_CMDLINE_LINUX_DEFAULT - add gpu_unlock.targets=$addr to it by hand, then: sudo update-grub"; return 1
    fi
    run sudo update-grub || { bad "update-grub failed"; return 1; }
    changed=1
  fi
  # keep the card powered and initialised at probe, or the unlock is undone (gpu-unlock #135, #206)
  if grep -qx 'options nvidia NVreg_DynamicPowerManagement=0x00 NVreg_GpuInitOnProbe=1' "$conf" 2>/dev/null; then
    ok "$conf already in place"
  elif (( DRY )); then ok "would write $conf: options nvidia NVreg_DynamicPowerManagement=0x00 NVreg_GpuInitOnProbe=1"; changed=1
  else
    ok "writing $conf"
    printf '%s\n' "# gpu-unlock: keep the dGPU powered (runtime D3 resets the unlock) and initialise it at probe," \
      "# so the first CUDA client does not re-latch the stock tensor rate (gpu-unlock #135, #206)" \
      "options nvidia NVreg_DynamicPowerManagement=0x00 NVreg_GpuInitOnProbe=1" | sudo tee "$conf" >/dev/null
    changed=1
  fi
  if (( changed )); then
    if command -v update-initramfs >/dev/null; then run sudo update-initramfs -u -k all
    else run sudo dracut --regenerate-all --force; fi
    REBOOT_FOR_UNLOCK=1
    ok "done - it takes effect after a reboot. Check it with:  journalctl -k -b | grep gpu_unlock"
  elif grep -q "gpu_unlock.targets=$addr" /proc/cmdline; then
    ok "already set up and active since this boot (journalctl -k -b | grep gpu_unlock shows what it did)"
  else
    REBOOT_FOR_UNLOCK=1
    ok "already set up - it takes effect after the next reboot"
  fi
  echo
  echo "      To undo gpu-unlock later:"
  echo "        sudo cp $bak /etc/default/grub && sudo update-grub      (or remove gpu_unlock.targets=$addr by hand)"
  echo "        sudo rm $conf"
  echo "        sudo apt-get remove gpu-unlock-dkms gpu-unlock-firmware"
  echo "        sudo update-initramfs -u -k all   and reboot"
}
(( UNLOCK )) && gpu_unlock

# --- Summary -----------------------------------------------------------------
echo
if (( DRY )); then echo "  Dry run finished - nothing was changed."
elif (( ${#FAILED[@]} )); then echo "  Done, except: ${FAILED[*]} (see the messages above; re-run the installer to retry)."
else echo "  Done."; fi
if (( START )); then echo "  AI Studio Hub: $URL"; else echo "  Start it with: $ROOT/linux/run.sh"; fi
(( DOCKER_FRESH )) && echo "  Log out and back in once so Docker works without sudo."
(( REBOOT_FOR_DRIVER )) && echo "  Reboot after the NVIDIA driver install, then run the installer again."
(( REBOOT_FOR_UNLOCK )) && echo "  Reboot to turn gpu-unlock on."
echo "  Re-run any time:  bash $ROOT/install.sh   (or: curl -fsSL $RAW_URL/install.sh | bash)"
echo
if (( START && BROWSER && ! DRY )) && command -v xdg-open >/dev/null && [[ -n ${DISPLAY:-}${WAYLAND_DISPLAY:-} ]]; then
  xdg-open "$URL" >/dev/null 2>&1 &
fi
return 0
}

main "$@"
