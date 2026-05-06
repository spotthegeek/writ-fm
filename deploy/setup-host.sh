#!/usr/bin/env bash
# Run once on the Docker Swarm node that will host writ-fm.
# Sets up the data directory and labels the node.
# Usage: ssh user@host "bash -s" < deploy/setup-host.sh
set -euo pipefail

REMOTE_DIR="/opt/writ-fm"
NODE_NAME="$(docker info --format '{{.Name}}')"

echo "==> Setting up ${REMOTE_DIR}"
mkdir -p "${REMOTE_DIR}/output"
mkdir -p "${REMOTE_DIR}/output/talk_segments"
mkdir -p "${REMOTE_DIR}/output/music_bumpers"
mkdir -p "${REMOTE_DIR}/output/ondemand"
mkdir -p "${REMOTE_DIR}/config"

echo "==> Labelling Swarm node '${NODE_NAME}' with role=writ-fm"
docker node update --label-add role=writ-fm "${NODE_NAME}"

echo ""
echo "Next steps:"
echo "  1. Copy config files:  scp config/*.yaml user@host:${REMOTE_DIR}/config/"
echo "  2. Copy stack file:    scp docker-stack.yml user@host:${REMOTE_DIR}/"
echo "  3. Create env file:    cp deploy/.env.production ${REMOTE_DIR}/.env.production"
echo "     then fill in the secrets (API keys, passwords)"
echo "  4. Deploy:             ssh user@host"
echo "     cd ${REMOTE_DIR}"
echo "     set -a && source .env.production && set +a"
echo "     docker stack deploy -c docker-stack.yml writ-fm"
