"""The assistant's language model: Ollama in its own container (ai-assistant-llm). It plans on the GPU when there is
room and on the CPU otherwise, and is unloaded the moment a plan is done, so the studio that renders gets the GPU."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path

import httpx

from ..config import load_settings

OLLAMA = "http://127.0.0.1:11434"
CONTAINER = "ai-assistant-llm"
IMAGE = "ollama/ollama:latest"
MODELS_DIR = Path.home() / ".cache" / "ai-tools" / "ollama"
GPU_NEEDS_MIB = 4500          # qwen3:4b with an 8k context, plus headroom


def _docker(*args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


async def _up() -> bool:
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            return (await c.get(f"{OLLAMA}/api/version")).status_code == 200
    except Exception:
        return False


async def ensure_running(model: str) -> None:
    """Start the container (created on first use) and pull the model if it is missing."""
    if not await _up():
        if not shutil.which("docker"):
            raise RuntimeError("Docker is needed for the assistant's language model")
        st = await asyncio.to_thread(_docker, "inspect", "-f", "{{.State.Status}}", CONTAINER)
        if st.returncode == 0:
            await asyncio.to_thread(_docker, "start", CONTAINER)
        else:
            MODELS_DIR.mkdir(parents=True, exist_ok=True)
            gpu = ["--gpus", "all"] if shutil.which("nvidia-smi") else []
            r = await asyncio.to_thread(
                _docker, "run", "-d", "--name", CONTAINER, *gpu, "-e", "OLLAMA_KEEP_ALIVE=0",
                "-e", "OLLAMA_NUM_PARALLEL=1", "-e", "OLLAMA_MAX_LOADED_MODELS=1", "-p", "127.0.0.1:11434:11434",
                "-v", f"{MODELS_DIR}:/root/.ollama", "--restart", "unless-stopped", IMAGE, timeout=900)
            if r.returncode != 0:
                raise RuntimeError(f"could not start the assistant's language model: {r.stderr.strip()[:300]}")
        for _ in range(60):
            if await _up():
                break
            await asyncio.sleep(1)
        else:
            raise RuntimeError("the assistant's language model did not start")
    async with httpx.AsyncClient(timeout=10) as c:
        tags = (await c.get(f"{OLLAMA}/api/tags")).json().get("models", [])
    if not any(t.get("name") in (model, f"{model}:latest") for t in tags):
        async with httpx.AsyncClient(timeout=httpx.Timeout(3600, connect=10)) as c:
            r = await c.post(f"{OLLAMA}/api/pull", json={"model": model, "stream": False})
            if r.status_code != 200:
                raise RuntimeError(f"could not download {model}: {r.text[:200]}")


def free_vram_mib() -> int:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5).stdout
        return int(out.strip().splitlines()[0])
    except Exception:
        return 0


def pick_device(mode: str | None = None) -> str:
    """auto: the GPU when it has room (no studio holding it), else the CPU - never pushes a studio out."""
    mode = (mode or load_settings().assistant_device or "auto").lower()
    if mode in ("gpu", "cpu"):
        return mode
    return "gpu" if free_vram_mib() >= GPU_NEEDS_MIB else "cpu"


async def chat(messages: list[dict], schema: dict | None = None, device: str = "cpu", keep: bool = False,
               model: str | None = None, temperature: float = 0.3) -> tuple[str, dict]:
    """One reply. keep=True holds the model for a follow-up call (a few seconds); unload() drops it."""
    model = model or load_settings().assistant_model
    body = {"model": model, "messages": messages, "stream": False, "think": False,
            "keep_alive": "30s" if keep else 0,
            "options": {"num_ctx": 8192, "temperature": temperature, **({"num_gpu": 0} if device == "cpu" else {})}}
    if schema:
        body["format"] = schema
    t = time.time()
    for attempt in range(2):            # one retry: a dropped connection while the model loads
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10)) as c:
                r = await c.post(f"{OLLAMA}/api/chat", json=body)
            break
        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError):
            if attempt:
                raise
            await asyncio.sleep(2)
    if r.status_code != 200:
        raise RuntimeError(f"language model error: {r.text[:300]}")
    d = r.json()
    info = {"seconds": round(time.time() - t, 1), "device": device, "model": model,
            "tokens_in": d.get("prompt_eval_count"), "tokens_out": d.get("eval_count")}
    return (d.get("message") or {}).get("content", ""), info


async def unload(model: str | None = None) -> None:
    model = model or load_settings().assistant_model
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            await c.post(f"{OLLAMA}/api/generate", json={"model": model, "keep_alive": 0})
    except Exception:
        pass


def parse_json(text: str) -> dict:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        a, b = text.find("{"), text.rfind("}")
        return json.loads(text[a:b + 1]) if a >= 0 and b > a else {}


def debug_dump(name: str, data: dict) -> None:
    """The model's raw replies of the last plan, for checking what it actually wrote (data/assistant/last-<name>.json)."""
    try:
        from ..config import DATA_DIR
        d = DATA_DIR / "assistant"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"last-{name}.json").write_text(json.dumps(data, indent=1, ensure_ascii=False), "utf-8")
    except Exception:
        pass
