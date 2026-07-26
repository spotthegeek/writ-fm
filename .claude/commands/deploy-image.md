Build the writ-fm Docker image from the current source and distribute it to all swarm nodes. Service restarts are done manually by the user after the image is distributed.

## Steps

### 1. Sync source to doc02
```bash
rsync -az --delete \
  --exclude='.git/' \
  --exclude='.venv/' \
  --exclude='station/kokoro/.venv/' \
  --exclude='output/' \
  --exclude='temp/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='*.pyo' \
  /code/writ-fm/ root@doc02:/opt/writ-fm/build/
```

### 2. Build on doc02
```bash
ssh root@doc02 'cd /opt/writ-fm/build && docker build -t writ-fm:latest .'
```

### 3. Distribute to doc03 and doc04 in parallel
```bash
ssh root@doc02 'docker save writ-fm:latest | gzip' | ssh root@doc03 'docker load' &
ssh root@doc02 'docker save writ-fm:latest | gzip' | ssh root@doc04 'docker load' &
wait
```

### 4. Confirm distribution
Report that the image has been built and distributed to all nodes. The user will handle service restarts manually.

## Notes
- The icecast service uses `libretime/icecast` (external image), no rebuild needed.
- `prune-images_system-prune` runs periodically on all nodes and will delete untagged/unused images.
- No registry is configured — images are distributed manually via `docker save | docker load`.
- Build node is doc02 (swarm manager). Source lives at `/opt/writ-fm/build/` on doc02 after sync.
