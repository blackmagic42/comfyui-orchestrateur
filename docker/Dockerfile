# syntax=docker/dockerfile:1.7
#
# Modified ComfyUI image — bundles the local ComfyUI/ core only.
# No custom_nodes, no models, no input/output data: those are mounted at runtime
# (or installed via ComfyUI-Manager once the container is up).
#
# Build context MUST be the creation-ops repo root, e.g.:
#     docker build -f scripts/docker/Dockerfile -t comfyui-creation-ops:latest .

ARG CUDA_VERSION=12.6.3
ARG UBUNTU_VERSION=22.04
ARG PYTORCH_INDEX=https://download.pytorch.org/whl/cu126

# ─── Stage 1: builder ──────────────────────────────────────────────────────────
# Compiles wheels and installs Python dependencies into a venv that we copy
# into the runtime stage. Keeps the runtime image free of build toolchains.
FROM nvidia/cuda:${CUDA_VERSION}-cudnn-runtime-ubuntu${UBUNTU_VERSION} AS builder

ARG PYTORCH_INDEX
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.11 python3.11-venv python3.11-dev python3-pip \
        build-essential git curl ca-certificates pkg-config \
        libgl1 libglib2.0-0 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN python3.11 -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
RUN pip install --upgrade pip wheel setuptools

# Torch first, on the matching CUDA index — pinning here avoids a later
# requirements pin pulling a CPU-only wheel.
RUN pip install --extra-index-url ${PYTORCH_INDEX} \
        torch torchvision torchaudio

WORKDIR /app

# Layer the requirements files first so dependency installs can be cached
# independently of the rest of the source tree.
COPY ComfyUI/requirements.txt /tmp/req-comfyui.txt
RUN pip install -r /tmp/req-comfyui.txt

# ─── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM nvidia/cuda:${CUDA_VERSION}-cudnn-runtime-ubuntu${UBUNTU_VERSION} AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility,video

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.11 python3.11-venv \
        libgl1 libglib2.0-0 ffmpeg tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY ComfyUI /app/ComfyUI

# Mount points for state that should outlive the container.
RUN mkdir -p /app/ComfyUI/models /app/ComfyUI/input /app/ComfyUI/output \
             /app/ComfyUI/temp /app/ComfyUI/user
VOLUME ["/app/ComfyUI/models", "/app/ComfyUI/input", "/app/ComfyUI/output", \
        "/app/ComfyUI/user"]

EXPOSE 8188

WORKDIR /app/ComfyUI
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3.11", "main.py", "--listen", "0.0.0.0", "--port", "8188"]
