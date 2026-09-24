#!/usr/bin/env python3
"""Hey Laya — local voice assistant. Entry point."""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

# Ensure project root on path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import actions
import tts as tts_mod
from audio import Recorder, play_wav, transcribe, wake_hit
from decide import GATE, decide
from llm import LLM
from replies import reply
from timers import TimerBank
from ui import StatusUI


class Assistant:
    def __init__(self, gate: float, stt_model: str, use_ui: bool = True):
        self.gate = gate
        self.stt_model = stt_model
        self.ui = StatusUI() if use_ui else None
        self.llm = LLM()
        self.timers = TimerBank()
        self.speaker = tts_mod.Speaker(play_wav)
        self._stop = threading.Event()
        self._router = None
        self._warm_done = threading.Event()

    # --- lifecycle ---------------------------------------------------------
    def start(self):
        # Warm TTS cache in background
        threading.Thread(target=self._warm, daemon=True).start()
        # Lazy-load Laya (torch is heavy)
        threading.Thread(target=self._load_router, daemon=True).start()
        # Optional LLM autostart — default OFF (avoids OOM next to torch)
        if os.environ.get("LLM_AUTOSTART", "0") not in ("0", "false"):
            threading.Thread(target=self.llm.ensure, daemon=True).start()
        if self.ui:
            self.ui.set_state("ready", "Warming TTS + loading Laya…")

    def _warm(self):
        try:
            n = tts_mod.warm()
            print(f"[tts] warmed {n} phrases")
        except Exception as e:
            print(f"[tts] warm failed: {e}")
        self._warm_done.set()

    def _load_router(self):
        try:
            from laya import Router

            print("[laya] loading Router(preload=True)…")
            self._router = Router(preload=True)
            print("[laya] ready")
        except Exception as e:
            print(f"[laya] load failed: {e}")
            self._router = None

    def shutdown(self):
        self._stop.set()
        self.timers.cancel_all()
        self.llm.shutdown()
        if self.ui:
            self.ui.destroy()

    # --- utterance pipeline ------------------------------------------------
    def handle_text(self, text: str) -> str:
        """Full pipeline for a transcribed (or --text) utterance."""
        text = (text or "").strip()
        if not text:
            return ""
        if self.ui:
            self.ui.set_state("exec", text[:60])

        # Wait briefly for Laya if still loading
        t0 = time.time()
        while self._router is None and time.time() - t0 < 30:
            time.sleep(0.2)
            if self.ui:
                self.ui.tick()

        spoken = ""
        if self._router is not None:
            try:
                result = decide(self._router, text, gate=self.gate)
            except Exception as e:
                print(f"[laya] decide error: {e}")
                result = {"actions": [], "needs_llm": True, "task": "none", "confidence": 0.0}
        else:
            result = {"actions": [], "needs_llm": True, "task": "none", "confidence": 0.0}

        if result.get("actions") and not result.get("needs_llm"):
            spoken = self._run_actions(result["actions"])
        else:
            # Compound: try first half as action, rest to LLM
            if result.get("actions"):
                spoken = self._run_actions(result["actions"])
            answer = self.llm.ask(text)
            spoken = f"{spoken} {answer}".strip() if spoken else answer

        if not spoken:
            spoken = reply("error")
        if self.ui:
            self.ui.set_state("ready", text[:60])
        return spoken

    def _run_actions(self, actions_list: list[dict]) -> str:
        parts = []
        for act in actions_list:
            if act.get("action") == "timer":
                minutes = float(act.get("minutes", 5))
                self.timers.set(minutes, lambda: self.speak(reply("timer.done")))
                parts.append(f"Timer set for {minutes:g} minutes.")
            else:
                parts.append(actions.run(act))
        return " ".join(p for p in parts if p) or reply("error")

    def speak(self, text: str):
        if self.ui:
            self.ui.set_state("exec", text[:60])
        self.speaker.say(text)
        if self.ui:
            self.ui.set_state("ready")

    # --- voice loops -------------------------------------------------------
    def run_ptt(self, ptt_key: str = "alt_r"):
        """Hold PTT key → record → transcribe → handle."""
        try:
            from pynput import keyboard
        except Exception as e:
            print(f"[ptt] pynput unavailable: {e}")
            return

        rec = Recorder()
        pressed = False

        def on_press(key):
            nonlocal pressed
            if key == getattr(keyboard.Key, ptt_key, None) and not pressed:
                pressed = True
                rec.start()
                if self.ui:
                    self.ui.set_state("listen", "PTT held — speak")

        def on_release(key):
            nonlocal pressed
            if key == getattr(keyboard.Key, ptt_key, None) and pressed:
                pressed = False
                wav = rec.stop()
                if self.ui:
                    self.ui.set_state("exec", "Transcribing…")
                text = transcribe(wav, self.stt_model) if wav else ""
                if text:
                    spoken = self.handle_text(text)
                    self.speak(spoken)

        with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
            while not self._stop.is_set():
                if self.ui:
                    self.ui.tick()
                time.sleep(0.02)
            listener.stop()

    def run_wake(self):
        """Always-listen wake word loop."""
        from audio import Recorder as Rec

        while not self._stop.is_set():
            if self.ui:
                self.ui.set_state("ready", "Say “Hey Laya”…")
                self.ui.tick()
            rec = Rec()
            rec.start()
            # Poll for stop while recording ~2.5s
            for _ in range(25):
                if self._stop.is_set():
                    rec.stop()
                    return
                if self.ui:
                    self.ui.tick()
                time.sleep(0.1)
            wav = rec.stop()
            if not wav:
                time.sleep(0.2)
                continue
            text = transcribe(wav, self.stt_model)
            if not wake_hit(text):
                continue
            # Command may be in same buffer after wake phrase
            from audio import WAKE

            cmd = WAKE.sub("", text).strip(" ,.!?")
            if self.ui:
                self.ui.set_state("listen", "Wake hit — speak command")
            if not cmd:
                # Open short PTT-style record for the actual command
                rec2 = Rec()
                rec2.start()
                time.sleep(3.0)
                wav2 = rec2.stop()
                cmd = transcribe(wav2, self.stt_model) if wav2 else ""
            if cmd:
                spoken = self.handle_text(cmd)
                self.speak(spoken)


def run_text_fixture(path: str, assistant: Assistant) -> int:
    """--fixture: run phrases through pipeline, print task/answer (no audio)."""
    p = Path(path)
    if not p.exists():
        print(f"fixture not found: {p}")
        return 1
    lines = [ln.strip() for ln in p.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    print(f"Running {len(lines)} fixture phrases…")
    fails = 0
    for ln in lines:
        spoken = assistant.handle_text(ln)
        print(f"  IN:  {ln}\n  OUT: {spoken}")
        if not spoken:
            fails += 1
    print(f"fixture done — {fails} empty")
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description="Hey Laya — local voice assistant")
    ap.add_argument("--text", help="Run one utterance and print reply (no audio loop)")
    ap.add_argument("--fixture", help="Run a fixture file of phrases (no audio loop)")
    ap.add_argument("--gate", type=float, default=float(os.environ.get("GATE", GATE)))
    ap.add_argument("--stt", default=os.environ.get("STT_MODEL", "base"))
    ap.add_argument("--no-ui", action="store_true")
    ap.add_argument("--wake-only", action="store_true")
    ap.add_argument("--ptt-only", action="store_true")
    ap.add_argument("--demo-tts", action="store_true", help="Warm TTS cache and exit")
    args = ap.parse_args()

    use_ui = not args.no_ui and not args.text and not args.fixture and not args.demo_tts
    assistant = Assistant(gate=args.gate, stt_model=args.stt, use_ui=use_ui)

    if args.demo_tts:
        n = tts_mod.warm()
        print(f"warmed {n} phrases → {tts_mod.CACHE}")
        return 0

    if args.fixture:
        assistant.start()
        # Wait for router
        t0 = time.time()
        while assistant._router is None and time.time() - t0 < 60:
            time.sleep(0.3)
        return run_text_fixture(args.fixture, assistant)

    if args.text:
        assistant.start()
        t0 = time.time()
        while assistant._router is None and time.time() - t0 < 60:
            time.sleep(0.3)
        spoken = assistant.handle_text(args.text)
        print(spoken)
        return 0

    # Voice mode
    assistant.start()
    mode = assistant.ui.mode() if assistant.ui else os.environ.get("MODE", "both")
    if args.wake_only:
        mode = "wake"
    if args.ptt_only:
        mode = "ptt"

    try:
        if mode == "ptt":
            assistant.run_ptt()
        elif mode == "wake":
            assistant.run_wake()
        else:
            # both: wake loop on main; PTT on side thread
            t = threading.Thread(target=assistant.run_ptt, daemon=True)
            t.start()
            assistant.run_wake()
    except KeyboardInterrupt:
        pass
    finally:
        assistant.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
