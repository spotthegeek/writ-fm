# WRIT-FM Session Summary: Linux Port & Installation

**Date:** Sunday, April 5, 2026
**Host System:** Linux (Debian/Ubuntu-based), 4GB RAM (Upgraded from 1GB)
**Current Status:** **LIVE** at `http://fm01.area4.net:8000/stream`

## 1. Environment & Prerequisites
- **Python:** 3.13.5 (Managed via `uv`)
- **System Dependencies:** `icecast2`, `ffmpeg` (with `libmp3lame`), `espeak-ng`.
- **Workspace:** All work performed in `/root/writ-fm/`.

## 2. Key Modifications for Linux Port
- **Icecast Config (`config/icecast.xml`):**
    - Updated paths to `/usr/share/icecast2/web` and `/usr/share/icecast2/admin`.
    - Set `logdir` to local `./logs` for permission-free operation.
    - **Password:** Synced with system default (`1cecast2`).
- **Streamer (`mac/stream_gapless.py`):**
    - Updated `ICECAST_PASS` default to `1cecast2`.
    - Verified `ffmpeg` piping works correctly on Linux.
- **LLM Helpers (`mac/content_generator/helpers.py`):**
    - Added support for **Gemini CLI** as a fallback/alternative to Claude.
    - Command used: `gemini --approval-mode plan -p [PROMPT]`
- **Operator Launcher (`run_operator.sh`):**
    - Updated to use the `gemini` CLI for autonomous maintenance.

## 3. TTS (Text-to-Speech) Resolution
- **Issue:** The original subprocess-based Kokoro calls failed due to OOM (1GB RAM) and ONNX Runtime crashes (SegFault 139).
- **Solution:** Now using 4GB RAM. Modified `mac/content_generator/talk_generator.py` to load the **Kokoro `KPipeline`** once in-process. This significantly reduces memory overhead and prevents process-spawning crashes.
- **Voices:** Currently using `bm_daniel` (Dr. Resonance) and `am_michael` (The Operator).

## 4. Operational State
- **Streamer:** Running in a background `tmux` session: `tmux attach -t writ`.
- **Content:** Initial segments (`show_intro`, `deep_dive`, `music_essay`) generated for the `sonic_archaeology` show.
- **Music:** Since `music-gen.server` is not installed, sample `.wav` files were copied from the system cache into `output/music_bumpers/sonic_archaeology/` to act as placeholder music.

## 5. Version Control
- Generated a new SSH key and updated the Git remote to `git@github.com:spotthegeek/writ-fm.git` to successfully push and sync our Linux-ported changes to the upstream repository.

## 6. Pending Tasks / Tomorrow's Iteration
- **Music Library:** User needs to point `mac/config.yaml` to a real music directory or install `music-gen.server` for AI bumpers.
- **Operator Tuning:** Verify the `run_operator.sh` logic with Gemini effectively restocks the queue without manual intervention.
- **Refactoring:** Continue optimizing the in-process TTS rendering to handle very long segments (2000+ words) without timeouts.

---
*To resume: Start by checking the tmux logs (`tmux capture-pane -t writ -p`) and verifying the current schedule (`uv run python mac/schedule.py now`).*
