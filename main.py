#!/usr/bin/env python3
"""Hey Laya — local voice assistant. Entry point."""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

# Native-runtime hygiene FIRST (before numpy/torch/ctranslate2 load):
# torch + ctranslate2 + OpenBLAS in one process oversubscribe small boxes
# and their duplicate OpenMP runtimes have SIGSEGV/SIGABRT history here.
# Cap fan-out and force pools to sleep (not spin) when idle.
os.environ.setdefault("OMP_NUM_THREADS", "4")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "4")
os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")
os.environ.setdefault("GOMP_SPINCOUNT", "1000")

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
from settings import load_settings
from timers import TimerBank
from ui import StatusUI

ECHO_GRACE = 0.6  # s after a reply ends before the mic re-opens (echo guard)
# UI pump rate for wait loops. ~14Hz: event latency stays <100ms while CPU
# stays near idle. (Draws are independently capped at 20Hz/8Hz — pumping
# faster than that only re-ran update() and burned CPU for nothing.)
PUMP_DT = 0.07


def _dbg(*args):
    """Routine chatter, off by default (LAYA_DEBUG=1 to see it)."""
    if os.environ.get("LAYA_DEBUG", "0").lower() not in ("0", "false", "", "no"):
        print(*args, flush=True)


def parse_hotkey(spec: str) -> str | None:
    """User text like 'ctrl+alt+l' → pynput GlobalHotKeys form '<ctrl>+<alt>+l'.

    Returns None when the spec is empty/invalid (caller then skips silently).
    Requires at least a modifier + a key so plain typing never summons us.
    """
    parts = [p.strip().lower().replace(" ", "_") for p in (spec or "").split("+")]
    parts = [p for p in parts if p]
    if not parts:
        return None
    mods = {
        "ctrl": "<ctrl>", "control": "<ctrl>",
        "alt": "<alt>", "option": "<alt>",
        "shift": "<shift>", "cmd": "<cmd>", "super": "<cmd>", "meta": "<cmd>", "win": "<cmd>",
    }
    named = {
        "space": "<space>", "enter": "<enter>", "tab": "<tab>",
        "esc": "<esc>", "escape": "<esc>", "up": "<up>", "down": "<down>",
        "left": "<left>", "right": "<right>", "home": "<home>", "end": "<end>",
        "page_up": "<page_up>", "page_down": "<page_down>",
        "insert": "<insert>", "delete": "<delete>", "caps_lock": "<caps_lock>",
        "scroll_lock": "<scroll_lock>", "num_lock": "<num_lock>",
        "pause": "<pause>", "print_screen": "<print_screen>",
    }
    out: list[str] = []
    for p in parts:
        if p in mods:
            out.append(mods[p])
        elif p in named:
            out.append(named[p])
        elif len(p) == 1 and (p.isalnum() or p in (";", "'", ",", ".", "/", "\\", "-", "=", "[", "]")):
            out.append(p)
        elif len(p) <= 4 and p.startswith("f") and p[1:].isdigit() and 1 <= int(p[1:]) <= 35:
            out.append(f"<{p}>")
        else:
            return None
    if len(out) < 2 or not any(o.startswith("<") for o in out):
        return None  # need at least modifier + key
    return "+".join(out)


class Assistant:
    def __init__(self, gate: float, stt_model: str, use_ui: bool = True):
        self.gate = gate
        self.stt_model = stt_model
        self.settings = load_settings()
        self.ui = (
            StatusUI(
                on_ptt_start=self.manual_ptt_start,
                on_ptt_end=self.manual_ptt_end,
                on_settings_change=self.on_settings_update,
                on_text=self.submit_text,
            )
            if use_ui
            else None
        )
        self.llm = LLM()
        self.timers = TimerBank()
        self.speaker = tts_mod.Speaker(play_wav)
        self._stop = threading.Event()
        self._router = None
        self._router_done = threading.Event()
        self._warm_done = threading.Event()
        self._manual_rec = None
        self._wake_prev = ""
        self._noise_gate = 0.003
        self._floor_samples: list[float] = []
        self._launch_listener = None
        self._tray_stop = None
        self._shut = False

    def on_settings_update(self, new_settings: dict):
        old = self.settings
        self.settings = new_settings
        if any(
            old.get(k) != new_settings.get(k)
            for k in ("llm_provider", "llm_api_url", "llm_api_key", "llm_api_model")
        ):
            self.llm.reset()  # re-resolve backend (local ↔ API key) on next ask
        if old.get("launch_hotkey") != new_settings.get("launch_hotkey"):
            self._refresh_launch_hotkey()

    # --- global launch hotkey: show/focus the app window -------------------
    def _refresh_launch_hotkey(self):
        self._stop_launch_hotkey()
        if self.ui is None:
            return
        spec = parse_hotkey((self.settings.get("launch_hotkey") or "").strip())
        if not spec:
            return
        try:
            from pynput import keyboard

            self._launch_listener = keyboard.GlobalHotKeys({spec: self._on_launch_hotkey})
            self._launch_listener.start()
            print(f"[hotkey] global launch hotkey: {spec}", flush=True)
        except Exception as e:
            print(f"[hotkey] launch hotkey unavailable: {e!r}", flush=True)
            self._launch_listener = None

    def _on_launch_hotkey(self):
        if self.ui:
            self.ui.toggle_visible()

    def _stop_launch_hotkey(self):
        listener, self._launch_listener = getattr(self, "_launch_listener", None), None
        if listener:
            try:
                listener.stop()
            except Exception:
                pass

    # --- manual PTT: UI button / Space — works on Wayland ------------------
    def manual_ptt_start(self):
        if self._manual_rec:
            return
        from audio import Recorder

        dev = self.settings.get("mic_device")
        self._manual_rec = Recorder(device=dev)
        self._manual_rec.start()
        if self.ui:
            self.ui.set_state("listen", "Listening… release when done")

    def manual_ptt_end(self):
        rec, self._manual_rec = self._manual_rec, None
        if not rec:
            return
        wav = rec.stop()
        if self.ui:
            self.ui.set_state("exec", "Processing…")
        threading.Thread(target=self._manual_finish, args=(wav,), daemon=True).start()

    def _manual_finish(self, wav):
        try:
            from audio import rms
            level = rms(wav) if wav else 0.0
            text = transcribe(wav, self.stt_model) if wav else ""
            print(f"[button] level={level:.3f} heard={text!r}", flush=True)
            if not text:
                if self.ui:
                    self.ui.set_state(
                        "ready",
                        "Heard nothing — hold longer, speak clearly"
                        if level < 0.005
                        else "No speech detected — please retry",
                    )
                return
            self.speak(self.handle_text(text))
        except Exception as e:
            print(f"[button] error: {e!r}", flush=True)
            if self.ui:
                self.ui.set_state("error", f"{e}"[:60])

    # --- lifecycle ---------------------------------------------------------
    def start(self):
        # Sweep orphaned mic slices from crashed runs (once, live ones kept)
        try:
            from audio import sweep_stale_wavs

            sweep_stale_wavs()
        except Exception:
            pass
        # Warm TTS cache in background
        threading.Thread(target=self._warm, daemon=True).start()
        # Pre-spawn the isolated STT worker + load tiny, so the first wake
        # slice doesn't pay spawn+load latency (and any spawn failure shows
        # up in the log at startup, not mid-conversation).
        threading.Thread(target=self._warm_stt, daemon=True).start()
        # Lazy-load Laya (torch is heavy)
        threading.Thread(target=self._load_router, daemon=True).start()
        # Optional LLM autostart — default OFF (avoids OOM next to torch)
        if os.environ.get("LLM_AUTOSTART", "0") not in ("0", "false"):
            threading.Thread(target=self.llm.ensure, daemon=True).start()
        self._refresh_launch_hotkey()
        if self.ui:
            self.ui.on_quit = self.shutdown
            try:
                from tray import start_tray

                self._tray_stop = start_tray(self.ui)
            except Exception as e:
                print(f"[tray] {e!r}", flush=True)
                self._tray_stop = None
            self.ui.set_state("ready", "Warming TTS + loading Laya…")

    def _warm(self):
        try:
            n = tts_mod.warm()
            print(f"[tts] warmed {n} phrases")
        except Exception as e:
            print(f"[tts] warm failed: {e}")
        self._warm_done.set()

    def _warm_stt(self):
        try:
            from audio import ensure_chime, transcribe_ex

            transcribe_ex(ensure_chime(), "tiny")
        except Exception as e:
            print(f"[stt] warm failed: {e}")

    def _load_router(self):
        try:
            try:
                import torch
                # Cap BLAS/OpenMP fan-out: an uncapped torch load can freeze
                # the whole box (oversubscription stalls the UI pump too).
                torch.set_num_threads(min(4, max(1, (os.cpu_count() or 4) - 2)))
                torch.set_num_interop_threads(1)
            except Exception:
                pass
            from laya import Router

            print("[laya] loading Router(preload=True)…")
            self._router = Router(preload=True)
            print("[laya] ready")
        except Exception as e:
            print(f"[laya] load failed: {e}")
            self._router = None
        finally:
            self._router_done.set()

    def is_ready(self) -> bool:
        """Models loaded: Laya router done AND voice warm done."""
        return self._warm_done.is_set() and self._router_done.is_set()

    def shutdown(self):
        if self._shut:
            return
        self._shut = True
        self._stop.set()
        self._stop_launch_hotkey()
        # Let in-flight record cycles observe _stop and close their
        # PortAudio streams before we tear the library down below.
        time.sleep(0.5)
        try:
            # Order matters: tear PortAudio down FIRST, while torch is still
            # fully alive. The reverse order (interpreter atexit lottery)
            # segfaults when both runtimes unload together.
            import sounddevice as sd

            sd._terminate()
        except Exception:
            pass
        if self._tray_stop:
            try:
                self._tray_stop()
            except Exception:
                pass
            self._tray_stop = None
        self.timers.cancel_all()
        try:
            from audio import close_stt

            close_stt()
        except Exception:
            pass
        self.llm.shutdown()
        if self.ui:
            self.ui.destroy()

    # --- utterance pipeline ------------------------------------------------
    def handle_text(self, text: str) -> str:
        """Full pipeline for a transcribed (or --text) utterance."""
        text = (text or "").strip()
        if len(text) > 220:
            # A hallucinated wall of text can hang the decision model at
            # 100% CPU; spoken commands are never this long — truncate.
            print(f"[laya] input truncated ({len(text)} chars)", flush=True)
            text = text[:220]
        if not text:
            return ""
        if self.ui:
            self.ui.set_state("exec", text[:60])

        # Wait briefly for Laya if still loading (no ui.tick here — may run off-main-thread)
        t0 = time.time()
        while self._router is None and time.time() - t0 < 30:
            time.sleep(0.2)

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
                self.timers.set(minutes, self._timer_done)
                if self.ui and hasattr(self.ui, "set_timer"):
                    self.ui.set_timer(minutes * 60)
                parts.append(f"Timer set for {minutes:g} minutes.")
            else:
                parts.append(actions.run(act))
        return " ".join(p for p in parts if p) or reply("error")

    def _timer_done(self):
        if self.ui:
            if hasattr(self.ui, "set_timer"):
                self.ui.set_timer(None)
            self.ui.set_state("ready", "Timer done")
        self.speak(reply("timer.done"))

    def speak(self, text: str):
        if self.ui:
            self.ui.set_state("exec", text[:60])
        self.speaker.say(text)
        if self.ui:
            self.ui.set_state("ready")

    # --- voice loops -------------------------------------------------------
    def run_ptt(self, ptt_key: str | None = None):
        """Hold PTT key → record → transcribe → handle."""
        if not ptt_key:
            ptt_key = self.settings.get("hotkey", "alt_r")

        if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
            print(
                f"[ptt] Wayland session detected — pynput hotkey [{ptt_key}] "
                "works when window is active or via the glowing orb widget",
                flush=True,
            )
        try:
            from pynput import keyboard
        except Exception as e:
            print(f"[ptt] pynput unavailable: {e}")
            return

        rec = Recorder(device=self.settings.get("mic_device"))
        pressed = False

        def on_press(key):
            nonlocal pressed
            target_key = getattr(keyboard.Key, self.settings.get("hotkey", "alt_r"), None)
            if key == target_key and not pressed:
                pressed = True
                rec.start()
                if self.ui:
                    self.ui.set_state("listen", "Listening…")

        def on_release(key):
            nonlocal pressed
            target_key = getattr(keyboard.Key, self.settings.get("hotkey", "alt_r"), None)
            if key == target_key and pressed:
                pressed = False
                wav = rec.stop()
                if self.ui:
                    self.ui.set_state("exec", "Processing…")
                text = transcribe(wav, self.stt_model) if wav else ""
                if text:
                    spoken = self.handle_text(text)
                    self.speak(spoken)

        with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
            while not self._stop.is_set():
                if self.ui:
                    self.ui.tick()
                time.sleep(0.05)
            listener.stop()

    def run_wake(self):
        """Always-listen wake word loop. Re-arms after every trigger."""
        from audio import Recorder as Rec

        while not self._stop.is_set():
            try:
                self._wake_cycle(Rec)
            except Exception as e:
                # One bad slice (mic hiccup, STT error) must never kill
                # the loop — log it and re-arm.
                print(f"[wake] cycle error (re-arming): {e!r}", flush=True)
                time.sleep(0.3)

    def _wake_cycle(self, Rec):
        if self.ui:
            if self.is_ready():
                self.ui.set_state("ready", "Say “Hey Laya”…")
                if hasattr(self.ui, "set_timer"):
                    self.ui.set_timer(self.timers.soonest_remaining())
            else:
                self.ui.set_state(
                    "loading",
                    "Loading Laya…" if not self._router_done.is_set() else "Warming voice…",
                )
            self.ui.tick()
        # Echo suppression: never open the mic while our own reply is
        # playing, plus a short grace after it ends (speaker/room tail).
        if self.speaker.playing.is_set():
            time.sleep(0.2)
            return
        if time.monotonic() - self.speaker.last_end < ECHO_GRACE:
            time.sleep(0.05)
            return
        rec = Rec(device=self.settings.get("mic_device"))
        rec.start()
        # Record a ~2.2s slice while pumping the UI. Wall-clock bounded —
        # never iteration-counted — so a slow renderer can never stretch
        # the slice and destabilize wake timing.
        t0 = time.monotonic()
        while time.monotonic() - t0 < 2.2:
            if self._stop.is_set():
                rec.stop()
                return
            self._pump()
            time.sleep(PUMP_DT)
        wav = rec.stop()
        over = time.monotonic() - t0 - 2.2
        if over > 1.0:
            print(f"[wake] slice overran by {over:.1f}s — UI pump too heavy?", flush=True)
        if self.speaker.playing.is_set():
            return  # playback started mid-slice: our own voice — discard
        if not wav:
            time.sleep(0.1)
            return
        from audio import rms

        level = rms(wav)
        if len(self._floor_samples) < 8:
            # Room calibration: the quietest of the first slices is the
            # ambient floor (a noisy room otherwise burns STT on every
            # slice and invites hallucinations).
            self._floor_samples.append(level)
            if len(self._floor_samples) == 8:
                floor = min(self._floor_samples)
                self._noise_gate = min(0.02, max(0.003, floor * 2.5))
                print(f"[wake] room floor={floor:.4f} → gate={self._noise_gate:.4f}",
                      flush=True)
        if level < self._noise_gate:
            # Silence / muted — keep _wake_prev so "hey|…laya" split
            # across two slices still matches.
            return

        # Fast wake transcription. The no-speech verdict rejects
        # prompt-biased hallucinations on noise/music/echo tails — the
        # classic "talking to herself" trigger. PTT keeps unfiltered STT.
        from audio import transcribe_ex
        text, is_speech = transcribe_ex(
            wav, model_size="tiny", prompt="Hey Laya, Hey Layla, OK Laya")
        text = (text or "").strip()
        if not is_speech:
            return  # junk audio: keep the short tail, never trigger
        if wake_hit(text):
            combined = text  # fresh hit: ignore any stale tail
        elif text and wake_hit(f"{self._wake_prev} {text}".strip()):
            combined = f"{self._wake_prev} {text}".strip()  # wake split across slices
        else:
            # No hit: carry only a short tail (a split word fragment),
            # never a whole command — stale text used to re-trigger and
            # mash every later utterance, so the wake word "worked once".
            self._wake_prev = text[-16:]
            return
        # Hit: the wake phrase is consumed — clear the carryover so this
        # utterance can never poison the NEXT detection cycle.
        self._wake_prev = ""

        _dbg(f"[wake] triggered by: {combined!r}")
        if self.ui:
            self.ui.set_state("listen", "Listening to your request…")

        from audio import WAKE
        cmd = WAKE.sub("", combined).strip(" ,.!?")

        # Everything slower than ~1s (command record, base STT, LLM) runs
        # in a worker while the main thread pumps the orb at 50fps —
        # animations never freeze mid-task.
        self._wake_prev = ""
        if cmd and len(cmd) >= 3:
            self._run_job(self._job_handle_text, cmd)
        else:
            self._run_job(self._job_listen_handle, Rec)

    # --- post-wake worker jobs (off the main thread) -------------------------
    def _pump(self):
        if self.ui:
            self.ui.tick()

    def _run_job(self, fn, *args):
        done = threading.Event()

        def _wrap():
            try:
                fn(*args)
            except Exception as e:
                print(f"[wake] job error: {e!r}", flush=True)
                if self.ui:
                    self.ui.set_state("error", f"{e}"[:60])
            finally:
                done.set()

        threading.Thread(target=_wrap, daemon=True).start()
        while not self._stop.is_set():
            self._pump()
            if done.wait(PUMP_DT):
                break

    def _job_handle_text(self, cmd: str):
        if self.ui:
            self.ui.set_state("exec", cmd[:60])
        # Attention chime, fire-and-forget: the mic is idle (main thread is
        # pumping, not recording), so it can't pollute anything.
        threading.Thread(target=self._chime_once, daemon=True).start()
        _dbg(f"[wake] executing: {cmd!r}")
        spoken = self.handle_text(cmd)
        self.speak(spoken)

    @staticmethod
    def _chime_once():
        try:
            from audio import ensure_chime, play_wav

            play_wav(str(ensure_chime()))
        except Exception:
            pass

    def _job_listen_handle(self, Rec):
        if self.ui:
            self.ui.set_state("listen", "Listening to your request…")
        # Chime first, blocking, off the main thread: the "I'm listening"
        # cue also guarantees our own voice is done before the mic opens.
        self._chime_once()
        # If a previous reply is still playing, wait it out (bounded) —
        # never record a command over our own voice.
        t_wait = time.monotonic()
        while self.speaker.playing.is_set() and time.monotonic() - t_wait < 4.0:
            if self._stop.is_set():
                return
            time.sleep(0.05)
        if self.speaker.playing.is_set():
            return  # still talking: drop it, re-arm quietly
        rec2 = Rec(device=self.settings.get("mic_device"))
        t_cmd = time.monotonic()
        rec2.start()
        t1 = time.monotonic()
        while time.monotonic() - t1 < 3.0:
            if self._stop.is_set():
                rec2.stop()
                return
            time.sleep(PUMP_DT)
        wav2 = rec2.stop()
        if self.speaker.playing.is_set() or self.speaker.last_end > t_cmd:
            return  # a reply (e.g. timer) started mid-command: discard ours
        if self._stop.is_set() or not wav2:
            return
        if self.ui:
            self.ui.set_state("exec", "Processing…")
        cmd = (transcribe(wav2, self.stt_model) or "").strip()
        if cmd:
            _dbg(f"[wake] executing: {cmd!r}")
            spoken = self.handle_text(cmd)
            self.speak(spoken)
        elif self.ui:
            self.ui.set_state("ready", "Didn't catch that — say “Hey Laya” and try again")

    # --- typed input: the orb's text box (mic-independent) --------------------
    def submit_text(self, text: str):
        text = (text or "").strip()
        if not text:
            return
        threading.Thread(target=self._job_text, args=(text,), daemon=True).start()

    def _job_text(self, text: str):
        try:
            if self.ui:
                self.ui.set_state("exec", text[:60])
            spoken = self.handle_text(text)
            self.speak(spoken)
        except Exception as e:
            print(f"[text] error: {e!r}", flush=True)
            if self.ui:
                self.ui.set_state("error", f"{e}"[:60])


def _singleton_lock() -> str | None:
    """Exit early if another hey-laya is already running.

    Two instances fight over the mic and double STT CPU — and the older one
    may run pre-fix code that re-triggers. Returns the holder pid or None.
    """
    try:
        import fcntl
    except ImportError:
        return None  # non-POSIX: no lock available
    global _LOCK_FD
    p = Path.home() / ".cache" / "hey-laya" / "app.lock"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        _LOCK_FD = open(p, "w")
        fcntl.flock(_LOCK_FD.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _LOCK_FD.write(str(os.getpid()))
        _LOCK_FD.flush()
        return None
    except OSError:
        try:
            return (p.read_text().strip() or "unknown").split()[0]
        except Exception:
            return "unknown"


_LOCK_FD = None


def _pick_stt_model(requested: str) -> str:
    """Auto-degrade to tiny on low-RAM boxes (halves STT CPU/RAM).

    Only the default ('base', whether from code or an untouched .env
    template) is ever downgraded — any deliberate choice (STT_MODEL set to
    something else, or any --stt flag) is always honored.
    """
    if requested != "base" or "--stt" in sys.argv:
        return requested
    try:
        with open("/proc/meminfo") as f:
            avail_kb = int(next(l for l in f if l.startswith("MemAvailable")).split()[1])
        if avail_kb < 10 * 1024 * 1024:
            print(f"[stt] low RAM ({avail_kb // 1024}MB avail) — using tiny "
                  f"instead of {requested} (override with --stt base)", flush=True)
            return "tiny"
    except Exception:
        pass
    return requested


class _Tee:
    """Fan stdout/stderr out to several streams (console + log file)."""

    def __init__(self, *streams):
        self._streams = streams

    def write(self, s):
        for st in self._streams:
            try:
                st.write(s)
            except Exception:
                pass
        self.flush()  # console is block-buffered under redirection; keep live output live

    def flush(self):
        for st in self._streams:
            try:
                st.flush()
            except Exception:
                pass


def _install_crash_logging():
    """Log everything to ~/.cache/hey-laya/app.log and dump Python stacks
    on fatal signals. Silent native deaths become diagnosable."""
    try:
        logdir = Path.home() / ".cache" / "hey-laya"
        logdir.mkdir(parents=True, exist_ok=True)
        log = open(logdir / "app.log", "a", buffering=1)
        log.write(f"\n===== hey-laya start {time.strftime('%F %T')} pid={os.getpid()} =====\n")
        sys.stdout = _Tee(sys.stdout, log)
        sys.stderr = _Tee(sys.stderr, log)
        import faulthandler

        faulthandler.enable(file=log)
    except Exception as e:
        print(f"[log] {e!r}", flush=True)


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
    _install_crash_logging()
    ap = argparse.ArgumentParser(description="Hey Laya — local voice assistant")
    ap.add_argument("--text", help="Run one utterance and print reply (no audio loop)")
    ap.add_argument("--fixture", help="Run a fixture file of phrases (no audio loop)")
    ap.add_argument("--gate", type=float, default=float(os.environ.get("GATE", GATE)))
    ap.add_argument("--stt", default=os.environ.get("STT_MODEL", "base"))
    ap.add_argument("--no-ui", action="store_true")
    ap.add_argument("--wake-only", action="store_true")
    ap.add_argument("--ptt-only", action="store_true")
    ap.add_argument("--demo-tts", action="store_true", help="Warm TTS cache and exit")
    ap.add_argument(
        "--mic-test",
        action="store_true",
        help="Record 3s, print mic level + transcription, exit (diagnose silent wake)",
    )
    args = ap.parse_args()

    use_ui = (
        not args.no_ui
        and not args.text
        and not args.fixture
        and not args.demo_tts
        and not args.mic_test
    )
    assistant = Assistant(
        gate=args.gate,
        stt_model=_pick_stt_model(args.stt),
        use_ui=use_ui,
    )

    if args.demo_tts:
        n = tts_mod.warm()
        print(f"warmed {n} phrases → {tts_mod.CACHE}")
        return 0

    if args.mic_test:
        import sounddevice as sd

        from audio import Recorder, rms, transcribe

        devs = sd.query_devices()
        default_in = sd.default.device[0]
        print(f"default input [{default_in}]: {devs[default_in]['name']}  "
              f"max_in={devs[default_in]['max_input_channels']}")
        print("speaking in 1s — record 3s…")
        r = Recorder()
        r.start()
        time.sleep(3.0)
        wav = r.stop()
        if not wav or not wav.exists() or wav.stat().st_size < 1024:
            print("FAIL: no audio frames captured — mic device wrong or busy")
            return 1
        level = rms(wav)
        text = transcribe(wav, args.stt)
        print(f"level={level:.3f}  transcribed={text!r}")
        print("OK: mic works" if level > 0.001 else "FAIL: captured silence (level≈0)")
        if level > 0.001 and not text:
            print("note: level OK but no text — say something clearly, or try --stt tiny")
        return 0

    if args.fixture:
        assistant.start()
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
    holder = _singleton_lock()
    if holder is not None:
        print(f"hey-laya is already running (pid {holder}) — not starting a second one",
              flush=True)
        print("kill the old one first if it is stuck: kill PID", flush=True)
        return 0
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
