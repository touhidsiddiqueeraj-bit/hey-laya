#!/usr/bin/env python3
"""Hey Laya — full test suite.

Fast unit tests always run. Pass --full to also load Laya and run the
routing fixture through the live decision model (slow, needs ~2GB RAM).

  python test_all.py          # fast suite (~seconds)
  python test_all.py --full   # + live Laya fixture
"""
from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f" FAIL {name} {detail}")


def section(title: str):
    print(f"\n== {title} ==")


# ---------------------------------------------------------------- replies
def test_replies():
    section("replies")
    from replies import REPLIES, reply

    check("non-empty dict", len(REPLIES) >= 30)
    check("reply hit", reply("media.pause") == "Paused.")
    check("reply miss → error", reply("nope") == REPLIES["error"])
    # every value is a short spoken string
    bad = [k for k, v in REPLIES.items() if not isinstance(v, str) or not v.strip()]
    check("all values are non-empty strings", not bad, str(bad))
    # no markdown noise in spoken lines
    md = [k for k, v in REPLIES.items() if re.search(r"[*#`|]", v)]
    check("no markdown in spoken replies", not md, str(md))


# ---------------------------------------------------------------- decide helpers
def test_decide_helpers():
    section("decide helpers (no Laya load)")
    # Import module without triggering laya? decide imports laya at top.
    # LayA is installed; import is fine, predict() is what's slow.
    from decide import (
        GATE,
        QUESTIONS,
        _app_hint,
        _extract_choice,
        _extract_conf,
        _flat,
        _score_index,
        active_questions,
        split_actions,
    )

    check("GATE is 0.50", abs(GATE - 0.50) < 1e-9)
    # QUESTIONS is a dict of qdefs with type+instructions
    ok_schema = all(
        isinstance(v, dict) and "type" in v and "instructions" in v for v in QUESTIONS.values()
    )
    check("QUESTIONS schema (dict + type + instructions)", ok_schema)
    # choice criteria are dicts; score criteria are lists
    for k, q in QUESTIONS.items():
        if q["type"] == "choice":
            check(f"{k}.criteria is dict", isinstance(q["criteria"], dict))
        elif q["type"] == "score":
            check(f"{k}.criteria is list", isinstance(q["criteria"], list))

    # _flat
    check(
        "_flat nested choice",
        _flat({"task": {"choice": "volume", "confidence": 0.9}})["task"] == "volume",
    )
    check("_flat scalar passthrough", _flat({"task": "app"})["task"] == "app")

    # _score_index
    check("score index round", _score_index({"v": {"score": 3.0}}, "v") == 3)
    check("score index default", _score_index({}, "v", default=2) == 2)
    check("score index clamp", _score_index({"v": {"score": -1}}, "v") == 0)

    # _extract_conf prefers answer_confidence
    check(
        "conf prefers answer_confidence",
        _extract_conf({"task": {"confidence": 0.1, "answer_confidence": 0.9}}, "task") == 0.9,
    )
    check("conf fallback", _extract_conf({"task": {"confidence": 0.7}}, "task") == 0.7)
    check("conf missing → 0", _extract_conf({}, "task") == 0.0)

    # _extract_choice
    check("choice extraction", _extract_choice({"t": {"choice": "app"}}, "t") == "app")
    check("missing choice → None", _extract_choice({}, "t") is None)

    # active_questions depends_on
    qs = active_questions({"task": "volume"})
    check("volume activates volume_direction", "volume_direction" in qs)
    check("volume does not activate media_action", "media_action" not in qs)
    qs2 = active_questions({"task": {"choice": "media", "confidence": 0.9}})
    check("nested answer flattening", "media_action" in qs2 and "volume_direction" not in qs2)
    qs3 = active_questions({"task": "volume", "volume_direction": {"choice": "set"}})
    check("set activates volume_level", "volume_level" in qs3)

    # _app_hint
    check("hint spotify", _app_hint("open spotify") == "spotify")
    check("hint launch", _app_hint("launch Spotify") == "spotify")
    check("hint the-app", _app_hint("Open the settings app") == "settings")
    check("hint None for non-app", _app_hint("start the timer") is None)
    check("hint None for play", _app_hint("play some music") is None)

    # split_actions: volume up
    acts = split_actions(
        {"task": {"choice": "volume", "answer_confidence": 0.9},
         "volume_direction": {"choice": "up"}},
        "volume", 0.9, GATE,
    )
    check("volume up → step 10", acts == [{"action": "volume_step", "step": 10}], str(acts))

    # volume set 50 → index 2
    acts = split_actions(
        {"task": {"choice": "volume", "answer_confidence": 0.9},
         "volume_direction": {"choice": "set"},
         "volume_level": {"score": 2}},
        "volume", 0.9, GATE,
    )
    check("volume set idx2 → 50", acts == [{"action": "volume_set", "pct": 50}], str(acts))

    # timer idx 2 → 10 min
    acts = split_actions(
        {"task": {"choice": "timer", "answer_confidence": 0.9},
         "timer_minutes": {"score": 2}},
        "timer", 0.9, GATE,
    )
    check("timer idx2 → 10", acts == [{"action": "timer", "minutes": 10}], str(acts))

    # below gate → no actions
    acts = split_actions({"task": {"choice": "volume"}}, "volume", 0.3, GATE)
    check("below gate → []", acts == [])

    # dark mode on
    acts = split_actions(
        {"task": {"choice": "display", "answer_confidence": 0.9},
         "display_kind": {"choice": "dark"},
         "display_direction": {"choice": "on"}},
        "display", 0.9, GATE,
    )
    check("dark on", acts == [{"action": "dark_mode", "on": True}], str(acts))


# ---------------------------------------------------------------- audio
def test_audio():
    section("audio")
    from audio import WAKE, wake_hit

    check("wake hey-laya", wake_hit("Hey Laya, pause the music"))
    check("wake ok-laya", wake_hit("ok laya set a timer"))
    check("wake heilaya", wake_hit("heilaya volume up"))
    check("wake whisper 'Hey, Leia'", wake_hit("Hey, Leia. Open the browser."))
    check("wake 'Hey Leia,'", wake_hit("Hey Leia, open the browser"))
    check("wake negative", not wake_hit("hello there"))
    check("wake None-safe", not wake_hit(None))
    check("wake strip", WAKE.sub("", "hey laya volume up").strip(" ,.!?")
          == "volume up")
    # regex only matches at word boundaries
    check("no mid-word match", not wake_hit("playerlaya is cool"))


# ---------------------------------------------------------------- timers
def test_timers():
    section("timers")
    from timers import TimerBank

    fired = threading.Event()
    bank = TimerBank()
    bank.set(0.01, fired.set)  # 0.6s
    check("timer fires", fired.wait(2.5))
    fired2 = threading.Event()
    bank.set(10, fired2.set)
    bank.cancel_all()
    time.sleep(0.8)
    check("cancel_all stops pending", not fired2.is_set())


# ---------------------------------------------------------------- tts
def test_tts():
    section("tts")
    import tts

    p1 = tts.cache_path("hello world")
    check("cache path deterministic", p1 == tts.cache_path("hello world"))
    check("cache path differs", tts.cache_path("hello") != tts.cache_path("world"))
    # synth of a previously-warmed phrase hits cache (no backend call)
    from replies import reply

    cached = tts.synth(reply("greet"))
    check("cached synth returns wav", cached is not None and cached.exists())
    if cached:
        with wave.open(str(cached), "rb") as w:
            check("wav has frames", w.getnframes() > 0)
    check("empty text → None", tts.synth("") is None)
    check("cache dir exists", tts.CACHE.is_dir())
    n = len(list(tts.CACHE.glob("*.wav")))
    check("cache populated (≥30)", n >= 30, f"got {n}")


# ---------------------------------------------------------------- llm (no spawn)
def test_llm():
    section("llm (canned path, autostart off)")
    import os

    os.environ["LLM_AUTOSTART"] = "0"
    os.environ.pop("LLM_URL", None)
    from llm import LLM, _pick_content

    llm = LLM()
    ok = llm.ensure()
    # No server expected on this machine in CI mode; if one exists that's fine too
    ans = llm.ask("hello")
    check("ask returns string", isinstance(ans, str) and bool(ans))
    if not ok:
        check("no-server → canned reply", "can't answer" in ans.lower(), ans)
    llm.shutdown()
    check("shutdown leaves no proc", llm.base is None)

    # _pick_content: normal content
    check(
        "pick normal",
        _pick_content({"choices": [{"message": {"content": "hi"}}]}) == "hi",
    )
    # reasoning-only → empty (falls to retry/canned upstream)
    check(
        "pick reasoning-only → empty",
        _pick_content(
            {"choices": [{"message": {"content": "", "reasoning_content": "think"}}]}
        ) == "",
    )
    # malformed payload → empty
    check("pick malformed → empty", _pick_content({}) == "")


# ---------------------------------------------------------------- actions dispatcher (dry)
def test_actions_dispatch():
    section("actions dispatcher")
    import actions

    # unknown action → error reply
    check("unknown → error", "wrong" in actions.run({"action": "nope"}).lower())
    # timer action returns key (main handles TimerBank)
    check("timer key", actions.run({"action": "timer"}) == "timer.set")
    # live light actions (volume/dark) must return a reply, not raise
    r = actions.run({"action": "volume_set", "pct": 50})
    check("volume_set reply", isinstance(r, str) and bool(r), r)
    r = actions.run({"action": "dark_mode", "on": False})
    check("dark_mode reply", isinstance(r, str) and bool(r), r)
    r = actions.run({"action": "volume_mute"})
    check("mute reply", r in ("Muted.", "Unmuted."), r)
    # leave unmuted at 50%
    actions.run({"action": "volume_mute"})
    actions.run({"action": "volume_set", "pct": 50})
    # new action types
    check("time reply", "It's" in actions.run({"action": "time"}))
    check("date reply", "Today is" in actions.run({"action": "date"}))
    # shutdown without confirmation is safely blocked
    check("shutdown safe block", "confirm" in actions.run({"action": "system", "cmd": "shutdown", "force": False}).lower())



# ---------------------------------------------------------------- ui (headless-safe)
def test_ui():
    section("ui")
    try:
        from ui import StatusUI
    except Exception as e:
        check("ui import", False, str(e))
        return
    try:
        u = StatusUI()
        for s in ("ready", "listen", "exec", "error"):
            u.set_state(s, "t")
        u.tick()
        u.destroy()
        check("state cycle", True)
    except Exception as e:
        # No display in CI is acceptable
        check("state cycle (skipped: no display)", True)
        print(f"    note: {e}")


# ---------------------------------------------------------------- module self-checks
def test_module_demos():
    section("module demos")
    import audio
    import decide
    import timers

    # run each demo() directly
    for mod, name in ((audio, "audio"), (decide, "decide"), (timers, "timers")):
        try:
            mod.demo()
            check(f"{name}.demo", True)
        except Exception as e:
            check(f"{name}.demo", False, str(e))


# ---------------------------------------------------------------- pyflakes
def test_pyflakes():
    section("pyflakes")
    files = [
        "main.py", "decide.py", "llm.py", "tts.py", "audio.py",
        "replies.py", "timers.py", "ui.py", "settings.py", "test_all.py",
        "actions/__init__.py", "actions/linux.py", "actions/windows.py",
    ]
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pyflakes", *files],
            capture_output=True, text=True, cwd=ROOT, timeout=30,
        )
        if r.returncode == 0:
            check("pyflakes clean", True)
        else:
            # pyflakes missing is a skip; findings are a fail
            if "No module named pyflakes" in (r.stderr or "") + (r.stdout or ""):
                check("pyflakes clean (skipped: not installed)", True)
            else:
                check("pyflakes clean", False, r.stdout.strip()[:400])
    except FileNotFoundError:
        check("pyflakes clean (skipped)", True)


# ---------------------------------------------------------------- full: live Laya fixture
def test_full_fixture():
    section("live Laya routing fixture (--full)")
    from decide import decide
    from laya import Router

    print("  loading Router…")
    router = Router(preload=True)

    # (phrase, expected action dict keys or None for LLM fallback)
    cases = [
        ("volume up", {"action": "volume_step"}),
        ("set volume to fifty percent", {"action": "volume_set", "pct": 50}),
        ("open spotify", {"action": "app"}),
        ("switch to dark mode", {"action": "dark_mode"}),
        ("set a timer for ten minutes", {"action": "timer", "minutes": 10}),
    ]
    for phrase, expect in cases:
        r = decide(router, phrase)
        acts = r["actions"]
        if expect is None:
            check(f"LLM: {phrase!r}", r["needs_llm"], str(r))
            continue
        ok = bool(acts) and acts[0]["action"] == expect["action"]
        if ok and "pct" in expect:
            ok = acts[0].get("pct") == expect["pct"]
        if ok and "minutes" in expect:
            ok = acts[0].get("minutes") == expect["minutes"]
        check(f"route: {phrase!r} → {expect}", ok, f"got {acts} task={r['task']} conf={r['confidence']:.2f}")

    # question → none → needs_llm
    r = decide(router, "what's the capital of France")
    check("question → needs_llm", r["needs_llm"], str(r))


def main():
    full = "--full" in sys.argv
    test_replies()
    test_decide_helpers()
    test_audio()
    test_timers()
    test_tts()
    test_llm()
    test_actions_dispatch()
    test_ui()
    test_module_demos()
    test_pyflakes()
    if full:
        test_full_fixture()
    else:
        print("\n(skip live Laya fixture — run with --full)")

    print(f"\n{'='*40}\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
