"""Audio I/O: PTT recording + wake-word listen loop + WAV playback."""
from __future__ import annotations

import json
import os
import re
import select
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
# Expanded fuzzy wake regex: captures "hey laya", "ok laya", "yo laya", "hi laya",
# "listen laya", "alright laya", "laya", plus common whisper misrecognitions
# (leia, layla, lia, liya, layer, maya, laura, raya, etc.)
WAKE = re.compile(
    r"\b(?:(?:hey|hi|hei|ok|okay|yo|alright|listen)\s*[,:]?\s*(?:laya|layla|leia|lia|liya|liah|lea|layer|laila|leya|maya|laura|raya)|laya)\b",
    re.IGNORECASE,
)


def wake_hit(text: str | None) -> bool:
    return bool(WAKE.search(text or ""))


class Recorder:
    """Record to temp WAV at 16k mono while a stop flag is set."""

    def __init__(self, sample_rate: int = SAMPLE_RATE, device: int | None = None):
        self.sample_rate = sample_rate
        self.device = device
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.path: Path | None = None

    def start(self):
        self._stop.clear()
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="laya_")
        os.close(fd)  # else every wake slice leaks an fd until the mic dies
        self.path = Path(path)
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
                device=self.device,
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


# STT runs in stt_worker.py (its own process): torch (Laya) + ctranslate2
# (faster-whisper) in one address space segfault — duplicate OpenMP
# runtimes. The worker dies alone if it ever must; the parent respawns it.
_WORKER_PATH = Path(__file__).parent / "stt_worker.py"
_REQUEST_TIMEOUT = 180.0


class _STTClient:
    """Persistent faster-whisper worker (JSON lines). Never raises."""

    def __init__(self):
        self._lock = threading.Lock()
        self._proc = None
        self._seq = 0

    def _spawn(self):
        import subprocess

        try:
            err = open(Path.home() / ".cache" / "hey-laya" / "app.log", "a", buffering=1)
        except Exception:
            err = None
        self._proc = subprocess.Popen(
            [sys.executable, "-u", str(_WORKER_PATH)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=err, text=True, bufsize=1,
        )

    def _kill(self):
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass

    def close(self):
        with self._lock:
            self._kill()

    def transcribe(self, wav, model_size="tiny", prompt=None) -> tuple[str, bool]:
        """(text, is_speech). Worker death/hang → ("", False); respawns next call."""
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                if self._proc is not None:
                    self._kill()  # reaped a dead worker; respawn below
                try:
                    self._spawn()
                except Exception as e:
                    print(f"[stt] worker spawn failed: {e!r}", flush=True)
                    return "", False
            assert self._proc is not None
            self._seq += 1
            req = json.dumps({"id": self._seq, "wav": str(wav),
                              "model": model_size, "prompt": prompt}) + "\n"
            try:
                assert self._proc.stdin is not None and self._proc.stdout is not None
                self._proc.stdin.write(req)
                self._proc.stdin.flush()
                try:
                    ready, _, _ = select.select([self._proc.stdout], [], [], _REQUEST_TIMEOUT)
                except Exception:
                    ready = [self._proc.stdout]  # non-POSIX fallback: blocking read
                if not ready:
                    raise TimeoutError(f"no reply in {_REQUEST_TIMEOUT:.0f}s")
                line = self._proc.stdout.readline()
                if not line:
                    raise EOFError("worker died")
                resp = json.loads(line)
                if resp.get("id") != self._seq:
                    raise ValueError("id mismatch")
                if resp.get("error"):
                    print(f"[stt] worker: {resp['error']}", flush=True)
                return resp.get("text", ""), bool(resp.get("ok"))
            except Exception as e:
                print(f"[stt] worker failed ({e!r}) — respawning next call", flush=True)
                self._kill()
                return "", False


_STT = _STTClient()


def close_stt():
    _STT.close()


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


def _cleanup_temp_wav(path: Path):
    """Delete our own Recorder temp files (never anything else)."""
    try:
        if path.parent == Path(tempfile.gettempdir()) and path.name.startswith("laya_"):
            path.unlink(missing_ok=True)
    except Exception:
        pass


def sweep_stale_wavs(max_age_s: float = 300.0):
    """One-time sweep of orphaned slices from crashed runs (not live ones)."""
    try:
        now = time.time()
        n = 0
        for p in Path(tempfile.gettempdir()).glob("laya_*.wav"):
            try:
                if now - p.stat().st_mtime > max_age_s:
                    p.unlink()
                    n += 1
            except Exception:
                pass
        if n:
            print(f"[mic] swept {n} stale slices", flush=True)
    except Exception:
        pass


def transcribe(wav_path: Path, model_size: str = "base", prompt: str | None = None) -> str:
    """Speech → text via the isolated worker ("" on empty audio/worker death)."""
    text, _ = _STT.transcribe(wav_path, model_size, prompt)
    _cleanup_temp_wav(Path(wav_path))
    return text


def transcribe_ex(wav_path: Path, model_size: str = "base",
                  prompt: str | None = None) -> tuple[str, bool]:
    """Transcribe + no-speech verdict, for the wake path.

    Returns (text, is_speech). is_speech is False for empty audio, worker
    failure, whisper-rated non-speech (mean no_speech_prob > 0.6 — the
    signature of prompt-biased hallucinations), or absurdly long output
    (a 2.2s slice physically holds ~10 words; more is a loop, never a
    command). Real speech here scores ≤0.22, so recall stays safe.
    """
    p = Path(wav_path)
    if not p.exists() or p.stat().st_size < 1024:
        _cleanup_temp_wav(p)
        return "", False  # mkstemp leaves a 0-byte file if the mic failed
    text, ok = _STT.transcribe(p, model_size, prompt)
    _cleanup_temp_wav(p)
    text = (text or "").strip()
    if len(text) > 150:
        return "", False
    return text, ok


CHIME = Path(__file__).parent / "cache" / "chime.wav"


def ensure_chime() -> Path:
    """Two-note attention chime (A5 → D6 bell), synthesized once locally."""
    try:
        if CHIME.exists() and CHIME.stat().st_size > 1024:
            with wave.open(str(CHIME), "rb") as w:
                if w.getnframes() > 0:
                    return CHIME
    except Exception:
        pass
    sr = 16000
    total = np.zeros(int(sr * 0.55), dtype=np.float64)
    for freq, t0, dur in ((880.0, 0.0, 0.30), (1174.66, 0.16, 0.34)):
        n = int(sr * dur)
        t = np.arange(n) / sr
        env = np.minimum(1.0, t / 0.008) * np.exp(-t * 5.0)
        s = int(sr * t0)
        total[s:s + n] += 0.32 * np.sin(2 * np.pi * freq * t) * env
    total = np.clip(total, -1.0, 1.0)
    CHIME.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(CHIME), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((total * 32767).astype(np.int16).tobytes())
    return CHIME


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
