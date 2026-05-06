#!/usr/bin/env bash
# Build the writ-fm Docker image and deploy to Docker Swarm.
#
# Modes:
#   Local build (Docker on this machine):
#     ./deploy/build.sh --host user@swarm-host [--version 1.0.0]
#
#   Remote build (no local Docker — builds on the swarm host itself):
#     ./deploy/build.sh --build-on-host user@swarm-host [--version 1.0.0]
#
# The remote build mode rsyncs the source to the host, builds there,
# then optionally deploys the stack.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# ── Defaults ──────────────────────────────────────────────────────────────────
IMAGE_NAME="writ-fm"
VERSION="${VERSION:-$(date +%Y%m%d-%H%M)}"
BUILD_HOST=""       # --build-on-host: build + deploy on this host
PUSH_HOSTS=()       # --host: transfer a locally-built image to these hosts
REMOTE_DIR="/opt/writ-fm"
DEPLOY=0

# ── Argument parsing ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --build-on-host) BUILD_HOST="$2"; shift 2 ;;
    --host)          PUSH_HOSTS+=("$2"); shift 2 ;;
    --version)       VERSION="$2"; shift 2 ;;
    --deploy)        DEPLOY=1; shift ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

IMAGE_TAG="${IMAGE_NAME}:${VERSION}"
cd "$PROJECT_ROOT"

# ── Remote build mode ─────────────────────────────────────────────────────────
if [[ -n "$BUILD_HOST" ]]; then
  echo "==> Remote build on ${BUILD_HOST}"
  echo "    Image: ${IMAGE_TAG}"

  REMOTE_SRC="${REMOTE_DIR}/src"
  ssh "$BUILD_HOST" "mkdir -p ${REMOTE_SRC}"

  echo "==> Syncing source to ${BUILD_HOST}:${REMOTE_SRC}"
  rsync -az --delete \
    --exclude='.git' \
    --exclude='.venv' \
    --exclude='station/kokoro/.venv' \
    --exclude='output' \
    --exclude='temp' \
    --exclude='.env' \
    --exclude='*.log' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    ./ "${BUILD_HOST}:${REMOTE_SRC}/"

  echo "==> Building image on ${BUILD_HOST}"
  ssh "$BUILD_HOST" "
    set -e
    cd ${REMOTE_SRC}
    docker build --platform linux/amd64 -t ${IMAGE_TAG} -t ${IMAGE_NAME}:latest .
    echo 'Build complete.'
  "

  if [[ "$DEPLOY" -eq 1 ]]; then
    echo "==> Deploying stack on ${BUILD_HOST}"
    ssh "$BUILD_HOST" "
      set -e
      cd ${REMOTE_DIR}
      if [ ! -f .env.production ]; then
        echo 'ERROR: ${REMOTE_DIR}/.env.production not found. Create it first.'
        exit 1
      fi
      set -a && source .env.production && set +a
      VERSION=${VERSION} docker stack deploy -c docker-stack.yml ${IMAGE_NAME}
    "
    echo "==> Stack deployed."
  else
    echo ""
    echo "Build done. To deploy:"
    echo "  ssh ${BUILD_HOST}"
    echo "  cd ${REMOTE_DIR}"
    echo "  set -a && source .env.production && set +a"
    echo "  VERSION=${VERSION} docker stack deploy -c docker-stack.yml ${IMAGE_NAME}"
  fi
  exit 0
fi

# ── Local build mode ──────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
  echo "ERROR: docker not found. Use --build-on-host user@host to build remotely."
  exit 1
fi

ARCHIVE="/tmp/${IMAGE_NAME}-${VERSION}.tar.gz"

echo "==> Building ${IMAGE_TAG}"
docker build --platform linux/amd64 -t "${IMAGE_TAG}" -t "${IMAGE_NAME}:latest" .

if [[ ${#PUSH_HOSTS[@]} -eq 0 ]]; then
  echo ""
  echo "No --host specified. To transfer manually:"
  echo "  docker save ${IMAGE_TAG} | gzip > ${ARCHIVE}"
  echo "  scp ${ARCHIVE} user@host:${REMOTE_DIR}/"
  echo "  ssh user@host \"docker load < ${REMOTE_DIR}/$(basename "$ARCHIVE")\""
  exit 0
fi

echo "==> Saving image"
docker save "${IMAGE_TAG}" | gzip > "${ARCHIVE}"
echo "    Archive size: $(du -sh "$ARCHIVE" | cut -f1)"

for HOST in "${PUSH_HOSTS[@]}"; do
  echo "==> Transferring and loading on ${HOST}"
  ssh "$HOST" "mkdir -p ${REMOTE_DIR}"
  scp "$ARCHIVE" "${HOST}:${REMOTE_DIR}/"
  ssh "$HOST" "docker load < ${REMOTE_DIR}/$(basename "$ARCHIVE") && rm ${REMOTE_DIR}/$(basename "$ARCHIVE")"
  echo "    Done: ${HOST}"
done

echo ""
echo "==> Image loaded on all hosts. To deploy:"
echo "  ssh <swarm-manager>"
echo "  cd ${REMOTE_DIR}"
echo "  set -a && source .env.production && set +a"
echo "  VERSION=${VERSION} docker stack deploy -c docker-stack.yml ${IMAGE_NAME}"
