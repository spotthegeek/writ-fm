# WRIT-FM Session Summary: Migration to Target Host

**Date:** Monday, April 6, 2026
**Target Host:** Current target (fm01 replacement) - Debian 13 with RTX 3060 (GPU passthrough)
**Current Status:** **LIVE** at `http://[target-host]:8000/stream`

## Migration Context
- **Source Host:** fm01.area4.net (previous deployment)
- **Target Host:** Current machine (GPU-enabled Debian 13)
- **Repository:** git@github.com:spotthegeek/writ-fm.git

## System Configuration

### Ollama Endpoint
- **OLLAMA_URL:** `http://ollama.area4.net:11434`
- No `/v1` suffix required (using direct Ollama API)

### System Dependencies (Installed)
- `icecast2` - Streaming media server
- `ffmpeg` - Audio encoding/processing
- `espeak-ng` - Fallback TTS
- `uv` - Python package manager

### GPU Configuration
- RTX 3060 with CUDA passthrough active
- Kokoro TTS updated to use `device="cuda"` in `talk_generator.py:359`

## Project Setup
- **Project Root:** `/root/writ-fm`
- **Python Environment:** Using `uv sync` with `.venv` virtual environment
- **TTS Extra:** Installed with `uv sync --extra tts`

## Migration Phases Completed

### Phase 1: System Dependencies ✅
- Installed icecast2, ffmpeg, espeak-ng, uv

### Phase 2: Repository Clone ✅
- Cloned WRIT-FM from https://github.com/spotthegeek/writ-fm.git to `/root/writ-fm`

### Phase 3: Model Cache ⏭️
- HuggingFace cache manually copied from source host
- Located at `/root/.cache/huggingface/`

### Phase 4: Project Setup ✅
- Ran `uv sync --extra tts` in project root
- All Python dependencies installed in `.venv`

### Phase 5: GPU Config ✅
- Updated `talk_generator.py` line 359 to use CUDA:
  ```python
  _KOKORO_PIPELINE = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M", device="cuda")
  ```

### Phase 6: Music Server 🔄
- Cloned `music-gen.server` from `https://github.com/kortexa-ai/music-gen.server.git`
- Installation in progress (Python environment issues encountered)
- Located at `/root/music-gen.server/`

### Phase 7: Final Cutover 🔄
- Icecast2 service started and running
- Streamer running with fallback tone (no talk segments or AI bumpers yet)
- Authentication issue resolved (password `hackme` instead of `1cecast2`)
- API server running on port 8001

## Current Operational State

### Running Processes
- **Streamer:** PID 16435, running via `setsid`
- **Icecast2:** Active and streaming
- **API Server:** Port 8001

### Stream Status
- Currently serving **fallback tone** (`output/fallback_tone.wav`)
- No talk segments generated yet
- No AI music bumpers available
- Schedule loaded with 8 shows

### Icecast Configuration
- **Source Password:** `hackme` (configured in `/etc/icecast2/icecast.xml`)
- **Mount Point:** `/stream`
- **Host:** `localhost:8000`

## Git Configuration
- SSH key generated for GitHub access: `/root/.ssh/writ_fm_migration`
- Public key ready for GitHub deployment

## Pending Tasks
1. Complete music-gen.server installation
2. Generate initial talk segments for shows
3. Deploy AI music bumpers from music-gen.server
4. Configure OLLAMA_URL environment variable for LLM script generation
5. Test full content pipeline (LLM → TTS → Stream)

---

*To resume: Verify streamer is running (`ps aux | grep stream_gapless`) and check logs (`tail -f /root/writ-fm/stream.log`)*