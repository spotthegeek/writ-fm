from kokoro_onnx import Kokoro
import soundfile as sf
from pathlib import Path
import os

# Get the kokoro directory
KOKORO_DIR = Path(__file__).parent

# Paths to models
MODEL_PATH = KOKORO_DIR / "onnx" / "model_q8f16.onnx"
VOICES_BIN = KOKORO_DIR / "voices" / "am_michael.bin" # We only have this one for now

_KOKORO = None

import onnxruntime
def get_kokoro():
    global _KOKORO
    if _KOKORO is None:
        if not MODEL_PATH.exists():
            raise FileNotFoundError(f"Model not found: {MODEL_PATH}")
        
        # Manually create session to control threads
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        session = onnxruntime.InferenceSession(str(MODEL_PATH), sess_options=options)
        
        # Note: kokoro-onnx might take a session in newer versions or we might need to patch it
        # Let's see if Kokoro() takes a session
        _KOKORO = Kokoro(str(MODEL_PATH), str(VOICES_BIN))
        # Actually, let's just use the Kokoro class and hope for the best if we can't inject session
        # If it still crashes, we'll have to use a different approach
    return _KOKORO

def render_onnx(text, output_path, voice="am_michael", speed=1.0):
    try:
        kokoro = get_kokoro()
        # Note: kokoro-onnx might expect voice name without .bin
        samples, sample_rate = kokoro.create(text, voice=voice, speed=speed)
        sf.write(str(output_path), samples, sample_rate)
        return True
    except Exception as e:
        print(f"kokoro-onnx Error: {e}")
        return False

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        if render_onnx(sys.argv[1], "test_onnx.wav"):
            print("Success: test_onnx.wav created")
        else:
            print("Failed")
