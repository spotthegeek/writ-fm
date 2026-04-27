# WRIT-FM Operator Session

You are operating the current Linux-first WRIT-FM stack.
Treat this as a maintenance and stocking session, not a speculative refactor session.

Priorities:

1. Keep the live stream healthy.
2. Keep the current and next scheduled shows stocked with talk segments and bumpers.
3. Avoid unnecessary restarts.
4. Detect drift between runtime behavior, config, and docs.

## Project Location

Run from the repo root: `/root/writ-fm`

## Current Reality To Assume

- The checked-in default station is `Crouch-FM`
- The checked-in timezone is `Australia/Adelaide`
- The current base rotation is `alien_theory`, `nosleep`, `sysadmin`, and `youtube-ai`
- Music generation is MiniMax/cloud-oriented now; do not assume ACE-Step is the primary path
- The admin UI runs from `admin/app.py`
- The streamer runs from `station/stream_gapless.py`

## 1. Health Check

Check the process and API surface first:

```bash
pgrep -af "stream_gapless|admin/app.py|api_server.py" || true
curl -sf http://localhost:8001/health || echo "API DOWN"
curl -sf http://localhost:8001/now-playing || true
curl -sf http://localhost:8000/status-json.xsl >/dev/null || echo "ICECAST STATUS DOWN"
```

If this machine is using systemd, also check:

```bash
systemctl status writ-fm.service writ-fm-admin.service --no-pager
```

Only restart services if there is a real fault.

## 2. Check The Current Show

```bash
uv run python station/schedule.py now
```

This should tell you what show is active and which content bucket should be stocked first.

## 3. Check Inventory

Talk segment inventory:

```bash
cd /root/writ-fm/station/content_generator
uv run python talk_generator.py --status
```

Music bumper inventory:

```bash
cd /root/writ-fm/station/content_generator
uv run python music_bumper_generator.py --status
```

Prioritize:

1. Current show
2. Next one or two scheduled shows
3. Any show below configured minimums

## 4. Generate Only What Is Needed

Talk:

```bash
cd /root/writ-fm/station/content_generator
uv run python talk_generator.py --show SHOW_ID --count 3
```

Or broader replenishment:

```bash
cd /root/writ-fm/station/content_generator
uv run python talk_generator.py --all --count 2
```

Music bumpers:

```bash
cd /root/writ-fm/station/content_generator
uv run python music_bumper_generator.py --show SHOW_ID --count 2
```

Do not over-generate if the scheduler is already keeping inventory healthy.

## 5. Listener Responses

If listener-message processing is relevant in the current deployment:

```bash
cd /root/writ-fm/station/content_generator
uv run python listener_response_generator.py
```

Do not hand-edit listener data unless you are repairing corruption.

## 6. Runtime Review

Look for:

- encoder restarts
- repeated generation failures
- missing inventory for the current show
- mismatched voice/backend behavior
- stale schedule/config assumptions

Useful checks:

```bash
curl -sf http://localhost:8001/schedule || true
find /root/writ-fm/output/talk_segments -maxdepth 2 -type f | wc -l
find /root/writ-fm/output/music_bumpers -maxdepth 2 -type f | wc -l
```

## 7. Drift Detection

Compare:

- runtime behavior
- `config/schedule.yaml`
- `README.md`
- `TODO.md`
- this file

Patch descriptive drift when you find it. Fix runtime drift only when it is safe and clearly necessary.

## Key Files

- `admin/app.py`
- `admin/scheduler.py`
- `config/schedule.yaml`
- `config/hosts.yaml`
- `config/segment_types.yaml`
- `config/show_taxonomy.yaml`
- `station/stream_gapless.py`
- `station/api_server.py`
- `station/content_generator/talk_generator.py`
- `station/content_generator/music_bumper_generator.py`
- `shared/hosts.py`
- `shared/settings.py`

## Notes

- The schedule config is still in a mixed migration state: canonical `hosts[]` plus some legacy flattened fields
- `WRIT_CONSUME_SEGMENTS` may still be `0`; verify before assuming consume-after-play
- The test suite currently passes and is a good pre/post-change sanity check:

```bash
cd /root/writ-fm
./.venv/bin/pytest -q
```
