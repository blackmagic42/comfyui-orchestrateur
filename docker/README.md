# Modified ComfyUI — Docker image

Bakes only the **modified ComfyUI core** (`ComfyUI/` source minus
`custom_nodes/`, `models/`, and runtime state). Custom nodes and weights are
expected to be installed at runtime — typically via ComfyUI-Manager once the
container is up, or by mounting them in.

## Build

The build context is the **repo root** (one level above `scripts/`):

```bash
# from anywhere
bash scripts/docker/build.sh

# or directly
docker build -f scripts/docker/Dockerfile -t comfyui-creation-ops:latest .
```

Build args (override with `--build-arg`):
- `CUDA_VERSION` (default `12.6.3`)
- `PYTORCH_INDEX` (default `https://download.pytorch.org/whl/cu128` — ships
  sm_120 kernels for RTX 50-series / Blackwell)
- `UBUNTU_VERSION` (default `22.04`)

For older GPUs (Ampere / Ada / Hopper) the cu126 wheel is smaller and works
fine — pass `--build-arg PYTORCH_INDEX=https://download.pytorch.org/whl/cu126`.

## Run

NVIDIA runtime + GPU passthrough required.

```bash
docker run --rm --gpus all -p 8188:8188 \
  -v "$(pwd)/ComfyUI/models:/app/ComfyUI/models" \
  -v "$(pwd)/ComfyUI/input:/app/ComfyUI/input" \
  -v "$(pwd)/ComfyUI/output:/app/ComfyUI/output" \
  -v "$(pwd)/ComfyUI/user:/app/ComfyUI/user" \
  -v "$(pwd)/ComfyUI/custom_nodes:/app/ComfyUI/custom_nodes" \
  comfyui-creation-ops:latest
```

Or via compose:

```bash
cd scripts/docker
docker compose up -d
```

ComfyUI listens on `http://localhost:8188`.

## What's baked in

- ComfyUI v0.19.5 core source from `ComfyUI/`
- `blueprints/` (pre-made workflow JSON)
- `middleware/cache_middleware.py`
- `alembic_db/` (DB migrations applied on startup)

## What's *not* baked in

- `custom_nodes/` — install via ComfyUI-Manager or mount from the host
- `models/`, `input/`, `output/`, `temp/`, `user/` — mount these
- `tests/`, `tests-unit/`, `.git/`, `__pycache__/` — excluded via `.dockerignore`

## Notes

- First build is dominated by the CUDA base image (~2 GB) and the torch wheel.
  Expect 5–10 min on a warm cache and ~6–8 GB image size.
- The Creation Ops Flask proxy (`web/`) is **not** included — it runs separately
  and proxies to this container's port 8188.
