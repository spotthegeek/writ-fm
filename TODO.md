# WRIT-FM Project To-Do List

## Migration Status

### Completed
- System dependencies installed (icecast2, ffmpeg, espeak-ng, uv)
- Project cloned and dependencies installed via `uv sync --extra tts`
- GPU configuration updated for Kokoro TTS (CUDA)
- Icecast2 service running and authenticated
- Streamer running with fallback tone
- API server running on port 8001

### In Progress
- Music-gen.server installation (Python environment issues)
- Talk segment generation pipeline

## Pending Tasks

### 1. Music-gen.server Deployment
- Complete installation of `/root/music-gen.server`
- Download ACE-Step models into `checkpoints/`
- Configure for GPU-accelerated music generation
- Connect to main streamer via `music_gen_client.py`

### 2. Content Generation Pipeline
- Configure `OLLAMA_URL=http://ollama.area4.net:11434` for LLM script generation
- Run initial talk segment generation for shows
- Test Kokoro TTS with CUDA acceleration
- Generate AI music bumpers via music-gen.server

### 3. Git Repository Sync
- Add SSH public key to GitHub for write access
- Sync git remote to SSH URL
- Push migration changes to upstream

### 4. Full Integration Testing
- Verify LLM → Script → TTS → Stream pipeline end-to-end
- Test AI music bumper generation and insertion
- Validate stream quality and transitions

---

## Completed Tasks ✓
- Automated Operator Tuning: Rewrote `helpers.py` to offload script generation to an external Ollama endpoint (`OLLAMA_URL`) to bypass cloud rate limits
- Fallback tone implemented in `stream_gapless.py` to prevent Icecast disconnects
- GPU Config: Updated `talk_generator.py` to use CUDA for Kokoro TTS
- Icecast2: Service started and configured with correct source password (`hackme`)
- Migration: Successfully migrated project to new target host with GPU