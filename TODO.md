# WRIT-FM Project To-Do List

## 1. Music Library & Bumpers
Configure `mac/config.yaml` to point to a real directory of your music, or investigate standing up the `music-gen.server` so the station can generate its own AI music bumpers dynamically (currently relying on sample `.wav` files).

## 2. API Server Validation
Ensure the `now_playing_server.py` is running smoothly and correctly serving the current track and segment metadata to the frontend.

---

## ✓ Completed Tasks
- Automated Operator Tuning: Rewrote `helpers.py` to offload script generation to an external Ollama endpoint (`OLLAMA_URL`) to bypass cloud rate limits, and implemented a fallback tone in `stream_gapless.py` to prevent Icecast disconnects.
- Rebuild WebUI: Created `index.html` mimicking the original WRIT-FM design, added a dark mode toggle, and integrated the volume control inside the main grey box.
- Sync Upstream GitHub Repository: Create SSH key, add to GitHub, and sync upstream origin GitHub repository (`spotthegeek/writ-fm`).
- Refactor the TTS rendering process in `mac/content_generator/talk_generator.py` and `listener_response_generator.py` to stream chunks to disk directly.
