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
# Fuzzy wake: whisper often renders "Laya" as Leia/Lia/Layla, and adds commas.
# "hey|hi|hei|ok|okay|yo" + optional comma/space + name; fused forms (heilaya) match too.
WAKE = re.compile(
    r"\b(?:hey|hi|hei|ok|okay|yo)\s*[,:]?\s*(?:laya|layla|leia|lia|liya|liah|lea)\b",
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
                print("[mic] no audio frames captured (device busy or muted?)", flush=True)
                return
            data = np.concatenate(frames, axis=0)
            with wave.open(str(self.path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(self.sample_rate)
                w.writeframes(data.tobytes())
        except Exception as e:
            # Headless / no mic: leave empty path
            print(f"[mic] record failed: {e!r}", flush=True)
            pass


# Whisper model cache — reloading per utterance made every wake cycle ~10s.
_MODELS: dict = {}


def rms(wav_path: Path) -> float:
    """Peak-normalized loudness 0..1 of a WAV — 'is the mic hearing me?'."""
    try:
        with wave.open(str(wav_path), "rb") as w:
            raw = w.readframes(w.getnframes())
        if not raw:
            return 0.0
        a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        return float(np.abs(a).mean()) if len(a) else 0.0
    except Exception:
        return 0.0


def transcribe(wav_path: Path, model_size: str = "base") -> str:
    """faster-whisper → text ("" if model missing or audio empty)."""
    p = Path(wav_path)
    if not p.exists() or p.stat().st_size < 1024:
        return ""  # mkstemp leaves a 0-byte file if the mic failed
    try:
        from faster_whisper import WhisperModel

        if model_size not in _MODELS:
            _MODELS[model_size] = WhisperModel(model_size, device="cpu", compute_type="int8")
        model = _MODELS[model_size]
        segments, _ = model.transcribe(
            str(p),
            language="en",
            condition_on_previous_text=False,  # stops silence-hallucination loops
            beam_size=1,  # greedy: ~2x faster on CPU, wake needs no beam search
        )
        return " ".join(s.text.strip() for s in segments).strip()
    except Exception as e:
        print(f"[stt] transcribe failed: {e!r}", flush=True)
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
