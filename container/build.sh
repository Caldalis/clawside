#!/usr/bin/env bash
# Build the clawside-agent container image. Idempotent — re-running just
# rebuilds layers that changed.
#
# Run from the repo root:    bash container/build.sh
# Or via make:               make build
#
# The build context is the repo root (".") so the Dockerfile's COPY paths
# (container/agent_runner/, container/skills/) resolve naturally.

set -euo pipefail

IMAGE_TAG="${CONTAINER_IMAGE:-clawside-agent:latest}"

cd "$(dirname "$0")/.."

echo ">> building ${IMAGE_TAG}"
docker build -t "${IMAGE_TAG}" -f container/Dockerfile .

echo ">> done. test with:  docker run --rm ${IMAGE_TAG} python -c 'print(\"ok\")'"
