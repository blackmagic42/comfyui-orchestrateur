# Modified ComfyUI — Docker image

This directory ships the `Dockerfile` that bakes the **local, modified** `ComfyUI/`
tree (including `custom_nodes/`, `blueprints/`, `middleware/`, and
`manager_requirements.txt`) into a runnable image.

Models, inputs, outputs and user data are **not** baked in — they are mounted at
runtime so rebuilds stay fast and large weights stay on the host.

## Build

The build context is the **repo root** (one level above `scripts/`). Either:

```bash
# from anywhere
bash scripts/docker/build.sh

# or directly
docker build -f scripts/docker/Dockerfile -t comfyui-creation-ops:latest .
```

Build args (override with `--build-arg`):
- `CUDA_VERSION` (default `12.6.3`)
- `PYTORCH_INDEX` (default `https://download.pytorch.org/whl/cu126`)
- `UBUNTU_VERSION` (default `22.04`)

For CUDA 13 hosts, pass `--build-arg PYTORCH_INDEX=https://download.pytorch.org/whl/cu130`
and a matching `CUDA_VERSION`.

## Run

NVIDIA runtime + GPU passthrough required (Docker Desktop on Windows uses WSL2).

```bash
docker run --rm --gpus all -p 8188:8188 \
  -v "$(pwd)/ComfyUI/models:/app/ComfyUI/models" \
  -v "$(pwd)/ComfyUI/input:/app/ComfyUI/input" \
  -v "$(pwd)/ComfyUI/output:/app/ComfyUI/output" \
  -v "$(pwd)/ComfyUI/user:/app/ComfyUI/user" \
  comfyui-creation-ops:latest
```

Or via compose:

```bash
cd scripts/docker
docker compose up -d
```

ComfyUI listens on `http://localhost:8188`.

## What's baked in

- ComfyUI v0.19.5 source from `ComfyUI/`
- All `custom_nodes/` present at build time (their `requirements.txt` are
  installed best-effort — a broken pin in one node won't fail the image)
- `blueprints/` (pre-made workflow JSON)
- `middleware/cache_middleware.py`
- `manager_requirements.txt`

## What's *not* baked in

- `models/`, `input/`, `output/`, `temp/`, `user/` — mount these
- `tests/`, `tests-unit/`, `.git/`, `__pycache__/` — excluded via `.dockerignore`
- LoRA weights (`*.safetensors`, `*.ckpt`, etc. inside custom_nodes)

## Notes

- First build is slow (CUDA base + torch + transformers + every custom-node
  dep). Expect 20–40 min and ~10–15 GB image size.
- The Creation Ops Flask proxy (`web/`) is **not** included — it runs separately
  and proxies to this container's port 8188.
