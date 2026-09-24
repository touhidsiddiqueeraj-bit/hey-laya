"""Audio I/O: PTT recording + wake-word listen loop + WAV playback."""
from __future__ import annotations

import re
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
# "Hey Laya" fuzzy wake variants
WAKE = re.compile(
    r"\b(hey\s*laya|hi\s*laya|ok\s*laya|heilaya|hey\s*layer|a\s*laya)\b",
    re.IGNORECASE,
)


def wake_hit(text: str | None) -> bool:
    return bool(WAKE.search(text or ""))


class Recorder:
    """Record to temp WAV at 16k mono while a stop flag is set."""

    def __init__(self, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = sample_rate
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.path: Path | None = None

    def start(self):
        self._stop.clear()
        self.path = Path(tempfile.mkstemp(suffix=".wav", prefix="laya_")[1])
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> Path | None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        return self.path if self.path and self.path.exists() else None

    def _run(self):
        try:
            import sounddevice as sd

            frames: list[np.ndarray] = []

            def cb(indata, _frames, _time, status):
                if not self._stop.is_set():
                    frames.append(indata.copy())

            with sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="int16",
                callback=cb,
            ):
                while not self._stop.is_set():
                    time.sleep(0.05)

            if not frames:
                return
            data = np.concatenate(frames, axis=0)
            with wave.open(str(self.path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(self.sample_rate)
                w.writeframes(data.tobytes())
        except Exception:
            # Headless / no mic: leave empty path
            pass


def transcribe(wav_path: Path, model_size: str = "base") -> str:
    """faster-whisper → text ("" if model missing)."""
    try:
        from faster_whisper import WhisperModel

        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        segments, _ = model.transcribe(str(wav_path), language="en")
        return " ".join(s.text.strip() for s in segments).strip()
    except Exception:
        return ""


def play_wav(path: str):
    """Play a wav via sounddevice (or ffplay fallback)."""
    try:
        import sounddevice as sd
        import soundfile as sf

        data, sr = sf.read(path, dtype="float32")
        sd.play(data, sr)
        sd.wait()
        return
    except Exception:
        pass
    try:
        import subprocess

        subprocess.run(
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path],
            check=False,
            timeout=30,
        )
    except Exception:
        pass


def listen_wake(stop: threading.Event, model_size: str = "base") -> str:
    """Short-buffer wake loop: record ~2.5s chunks, transcribe, return first hit + rest."""
    while not stop.is_set():
        rec = Recorder()
        rec.start()
        time.sleep(2.5)
        wav = rec.stop()
        if not wav:
            time.sleep(0.2)
            continue
        text = transcribe(wav, model_size)
        if wake_hit(text):
            # Strip wake phrase; remainder is the command (may be empty → open PTT)
            stripped = WAKE.sub("", text).strip(" ,.!?")
            return stripped
        # keep listening
    return ""


def demo():
    # Self-check: wake regex
    assert wake_hit("Hey Laya, pause the music")
    assert wake_hit("ok laya set a timer")
    assert not wake_hit("hello there")
    print("audio.demo OK")


if __name__ == "__main__":
    demo()
