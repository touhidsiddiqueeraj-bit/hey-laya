"""TTS: warm disk cache + live Kokoro (in-process venv) + Polaris :7802 fallback."""
from __future__ import annotations

import hashlib
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import requests

from replies import REPLIES, reply

CACHE = Path(__file__).parent / "cache" / "tts"
VOICE = os.environ.get("TTS_VOICE", "af_heart")
POLARIS = os.environ.get("POLARIS_TTS_URL", "").rstrip("/")

# Kokoro interpreter: KOKORO_PY env → known-good local venv → this venv (kokoro in requirements).
KOKORO_PY = Path(
    os.environ.get("KOKORO_PY")
    or Path.home() / "Downloads/Videos/remotion/Kokoro-TTS-Local/.venv/bin/python3.11"
)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def cache_path(text: str) -> Path:
    return CACHE / f"{_hash(text)}.wav"


def _wav_ok(p: Path) -> bool:
    try:
        with wave.open(str(p), "rb") as w:
            return w.getnframes() > 0
    except Exception:
        return False


def _kokoro_wav(text: str, out: Path, voice: str = VOICE) -> bool:
    """Render via Kokoro in the known-good venv (subprocess, keeps main deps light)."""
    py = KOKORO_PY if KOKORO_PY.exists() else Path(sys.executable)
    script = f"""
import sys
from pathlib import Path
out = Path({str(out)!r})
try:
    from kokoro import KPipeline
    import soundfile as sf
    import numpy as np
    pipe = KPipeline(lang_code="a")
    chunks = []
    for _, _, audio in pipe({text!r}, voice={voice!r}, speed=1.0):
        chunks.append(audio)
    if not chunks:
        sys.exit(2)
    y = np.concatenate(chunks)
    sf.write(str(out), y, 24000)
    sys.exit(0)
except Exception as e:
    print(e, file=sys.stderr)
    sys.exit(1)
"""
    try:
        cmd = [str(py), "-c", script]
        if shutil.which("nice"):
            # Don't let a cold model load freeze the orb: yield the CPU.
            cmd = ["nice", "-n", "10"] + cmd
        r = subprocess.run(
            cmd,
            capture_output=True,
            timeout=180,  # cold kokoro load can exceed 60s while the router loads
        )
        if r.returncode != 0:
            print(f"[tts] kokoro rc={r.returncode}: {r.stderr.decode()[:200]}", flush=True)
        return r.returncode == 0 and _wav_ok(out)
    except Exception as e:
        print(f"[tts] kokoro failed: {e!r}", flush=True)
        return False


def _polaris_tts(text: str, out: Path) -> bool:
    if not POLARIS:
        return False
    try:
        r = requests.post(
            POLARIS + "/audio/tts",
            json={"text": text, "voice": VOICE, "speed": 1.0, "format": "wav"},
            timeout=30,
        )
        if r.status_code != 200:
            return False
        data = r.json()
        if data.get("file") and Path(data["file"]).exists():
            out.write_bytes(Path(data["file"]).read_bytes())
            return _wav_ok(out)
        if data.get("audioB64"):
            import base64

            out.write_bytes(base64.b64decode(data["audioB64"]))
            return _wav_ok(out)
    except Exception:
        return False
    return False


def synth(text: str, voice: str = VOICE) -> Path | None:
    """Return path to wav for text (cache → polaris → kokoro)."""
    if not text or not text.strip():
        return None
    out = cache_path(text)
    if _wav_ok(out):
        return out
    CACHE.mkdir(parents=True, exist_ok=True)
    if _polaris_tts(text, out):
        return out
    if _kokoro_wav(text, out, voice=voice):
        return out
    return None


def warm(count: int | None = None) -> int:
    """Pre-synth canned replies so common phrases are instant."""
    items = list(REPLIES.values())
    if count:
        items = items[:count]
    n = 0
    for t in items:
        if synth(t):
            n += 1
    return n


class Speaker(threading.Thread):
    """Async playback queue so UI/actions never block on TTS."""

    def __init__(self, play_fn):
        super().__init__(daemon=True)
        self.q: queue.Queue[str | None] = queue.Queue()
        self._play = play_fn
        self.playing = threading.Event()  # set only while audio is on the speakers
        self.last_end = 0.0  # monotonic time the last playback finished (echo guard)
        self.start()

    def say(self, text: str):
        if text:
            self.q.put(text)

    def run(self):
        while True:
            text = self.q.get()
            if text is None:
                break
            path = synth(text)
            if path:
                self.playing.set()
                try:
                    self._play(str(path))
                finally:
                    self.playing.clear()
                    self.last_end = time.monotonic()
            else:
                print(f"[tts] no audio for {text!r}", flush=True)


if __name__ == "__main__":
    # Self-check: cache hit path for a known reply
    t = reply("greet")
    p = synth(t)
    print("synth →", p, "ok=" + str(bool(p)))
    print(f"warm cache dir: {CACHE} ({len(list(CACHE.glob('*.wav')))} files)")
    print("tts.demo OK" if p else "tts.demo PARTIAL (no voice backend yet)")
