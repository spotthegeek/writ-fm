# WRIT-FM Project To-Do List

## 1. Music Library & Bumpers
Configure `mac/config.yaml` to point to a real directory of your music, or investigate standing up the `music-gen.server` so the station can generate its own AI music bumpers dynamically (currently relying on sample `.wav` files).

## 2. Automated Operator Tuning
Test and verify that the updated `run_operator.sh` script (using the Gemini CLI) successfully and autonomously restocks the queue with new talk segments and listener mailbag responses without manual intervention.

## 3. API Server Validation
Ensure the `now_playing_server.py` is running smoothly and correctly serving the current track and segment metadata to the frontend.

## 4. Sync Upstream GitHub Repository
Create SSH key, add to GitHub, and sync upstream origin GitHub repository (`spotthegeek/writ-fm`).

---

## ✓ Completed Tasks
- Refactor the TTS rendering process in `mac/content_generator/talk_generator.py` and `listener_response_generator.py` to stream chunks to disk directly.
