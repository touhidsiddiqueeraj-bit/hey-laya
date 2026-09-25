"""Isolated STT worker: faster-whisper lives in its OWN process.

Rationale: torch (Laya) + ctranslate2 (faster-whisper) in one address space
segfault — duplicate OpenMP runtimes. Observed as random SIGSEGV/SIGABRT
~1min after load, i.e. exactly when STT first runs alongside torch.
Decoupling the address spaces kills the whole crash class; if the worker
ever dies, the parent respawns it and the loop re-arms.

Protocol (JSON lines over stdio; stderr is free-form logs):
  -> {"id": 7, "wav": "/tmp/laya_x.wav", "model": "tiny", "prompt": "..."|null}
  <- {"id": 7, "text": "...", "ok": true, "error": null}
ok=false means whisper itself rates the audio non-speech (hallucination
guard). A crash/timeout surfaces as ok=false + error set (parent respawns).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _read_wav(path: str):
    p = Path(path)
    if not p.exists() or p.stat().st_size < 1024:
        return None  # 0-byte file: mic captured nothing
    return p


def main() -> int:
    import faulthandler

    faulthandler.enable(file=sys.stderr)
    try:
        from faster_whisper import WhisperModel
    except Exception as e:
        print(f"[stt-worker] import failed: {e!r}", file=sys.stderr, flush=True)
        return 1

    models: dict = {}
    stdin = sys.stdin
    while True:
        line = stdin.readline()
        if not line:
            return 0  # parent went away (EOF): exit quietly
        try:
            req = json.loads(line)
        except Exception:
            continue
        rid = req.get("id")
        resp = {"id": rid, "text": "", "ok": False, "error": None}
        try:
            wav = str(req.get("wav") or "")
            size = str(req.get("model") or "tiny")
            prompt = req.get("prompt")
            if _read_wav(wav) is None:
                resp["error"] = "empty audio"
            else:
                if size not in models:
                    models[size] = WhisperModel(
                        size, device="cpu", compute_type="int8", cpu_threads=1)
                model = models[size]
                kwargs: dict = {
                    "language": "en",
                    "condition_on_previous_text": False,
                    "beam_size": 1,
                }
                if prompt:
                    kwargs["initial_prompt"] = prompt
                segments, _ = model.transcribe(wav, **kwargs)
                # Bound the generator: degenerate buffers can yield endless
                # hallucinated segments and wedge the call forever.
                out, chars = [], 0
                for s in segments:
                    out.append(s)
                    chars += len(s.text)
                    if len(out) >= 60 or chars > 600:
                        break
                if out:
                    resp["text"] = " ".join(s.text.strip() for s in out).strip()
                    ns = sum(s.no_speech_prob for s in out) / len(out)
                    resp["ok"] = ns <= 0.6
        except Exception as e:
            resp["error"] = f"{e.__class__.__name__}: {e}"[:200]
        try:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
        except Exception:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
