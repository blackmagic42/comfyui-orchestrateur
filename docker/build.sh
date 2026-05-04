#!/usr/bin/env bash
# Build the modified-ComfyUI Docker image.
#
# Run from the creation-ops repo root, or from anywhere — the script resolves
# its own location and uses the parent of `scripts/` as the build context so
# the local ComfyUI/ tree is bundled exactly as it sits on disk.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

IMAGE_NAME="${IMAGE_NAME:-comfyui-creation-ops}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
CUDA_VERSION="${CUDA_VERSION:-12.6.3}"
PYTORCH_INDEX="${PYTORCH_INDEX:-https://download.pytorch.org/whl/cu128}"

echo "Build context : ${REPO_ROOT}"
echo "Image         : ${IMAGE_NAME}:${IMAGE_TAG}"
echo "CUDA          : ${CUDA_VERSION}"

docker build \
    -f "${SCRIPT_DIR}/Dockerfile" \
    -t "${IMAGE_NAME}:${IMAGE_TAG}" \
    --build-arg "CUDA_VERSION=${CUDA_VERSION}" \
    --build-arg "PYTORCH_INDEX=${PYTORCH_INDEX}" \
    "${REPO_ROOT}"
