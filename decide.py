"""Laya decision layer — ported from hey-jev QUESTIONS + decide/split_actions."""
from __future__ import annotations

from laya import Router

# Decision gate: answers below this confidence fall through to LLM.
# Laya's calibrated `answer_confidence` is the documented gating number.
# The English checkpoint reports ~0.55–0.99 on task; 0.50 keeps clear commands
# while "none" for questions still falls through (those score >0.99 on none).
GATE = 0.50

# Score criteria index (0-based) → system percentages / minutes
VOL = [10, 25, 50, 75, 100]
TIMER_MIN = [1, 5, 10, 15, 30]

# App targets shared across open/focus choices (Linux-friendly, cross-platform names)
APPS = {
    "spotify": "spotify",
    "slack": "slack",
    "chrome": "google-chrome",
    "firefox": "firefox",
    "vscode": "code",
    "terminal": "org.gnome.Terminal",
    "files": "nautilus",
    "calculator": "gnome-calculator",
    "settings": "gnome-control-center",
}

QUESTIONS = {
    "task": {
        "type": "choice",
        "instructions": "What should Laya do? Use the transcript in `transcript`.",
        "criteria": {
            "app": "user says open/launch/start/run an application by name (open Slack, launch Spotify, focus the terminal)",
            "volume": "change system volume level up/down/mute/set",
            "display": "change screen brightness or switch dark/light theme",
            "media": "playback control only: play, pause, next, previous, stop — not opening apps",
            "system": "lock, logout, shutdown, reboot, or sleep the computer",
            "timer": "set a timer or countdown alarm",
            "none": "not a command — general conversation or question",
        },
    },
    "app_action": {
        "type": "choice",
        "instructions": "Open the app or just focus it if already running?",
        "criteria": {
            "open": "launch a new instance or start it",
            "focus": "bring the existing window to front",
        },
        "depends_on": {"task": "app"},
    },
    "app_target": {
        "type": "choice",
        "instructions": "Which application does the transcript name or imply?",
        "criteria": {k: k for k in APPS.keys()},
        "depends_on": {"task": "app"},
    },
    "volume_direction": {
        "type": "choice",
        "instructions": "Raise, lower, mute, or set absolute volume?",
        "criteria": {
            "up": "increase volume",
            "down": "decrease volume",
            "mute": "toggle or force mute",
            "set": "set to a specific level",
        },
        "depends_on": {"task": "volume"},
    },
    "volume_level": {
        "type": "score",
        "instructions": "Target volume level from quiet to loud as a percentage of max.",
        "criteria": ["10% very quiet", "25% quiet", "50% medium", "75% loud", "100% full"],
        "depends_on": {"task": "volume", "volume_direction": "set"},
    },
    "display_kind": {
        "type": "choice",
        "instructions": "Brightness or dark mode?",
        "criteria": {
            "brightness": "screen backlight level",
            "dark": "system dark / light theme",
        },
        "depends_on": {"task": "display"},
    },
    "display_direction": {
        "type": "choice",
        "instructions": "Which way should it change?",
        "criteria": {
            "up": "increase or turn on",
            "down": "decrease",
            "on": "enable",
            "off": "disable",
        },
        "depends_on": {"task": "display"},
    },
    "media_action": {
        "type": "choice",
        "instructions": "What media playback action does the transcript ask for?",
        "criteria": {
            "play": "start or resume playback",
            "pause": "pause playback",
            "next": "skip to next track",
            "previous": "go to previous track",
            "stop": "stop playback",
        },
        "depends_on": {"task": "media"},
    },
    "system_action": {
        "type": "choice",
        "instructions": "Which system action does the transcript ask for?",
        "criteria": {
            "lock": "lock the session",
            "logout": "log the user out",
            "shutdown": "power off",
            "reboot": "restart",
            "sleep": "suspend",
        },
        "depends_on": {"task": "system"},
    },
    "timer_minutes": {
        "type": "score",
        "instructions": "How many minutes for the timer? Use the duration mentioned in the transcript.",
        "criteria": ["1 minute", "5 minutes", "10 minutes", "15 minutes", "30 minutes"],
        "depends_on": {"task": "timer"},
    },
    # Compound: Laya noul is flaky on English checkpoint — use 2-option choice.
    "compound": {
        "type": "choice",
        "instructions": "Is this one command or a compound request with two parts?",
        "criteria": {
            "single": "one command only",
            "double": "two commands in one utterance",
        },
        "depends_on": {"task": "none"},
    },
}

# Which questions are active given prior answers (depends_on matches flat choice/score values)
def active_questions(state: dict) -> dict[str, dict]:
    flat = _flat(state)
    out = {}
    for key, q in QUESTIONS.items():
        dep = q.get("depends_on") or {}
        if not all(flat.get(k) == v for k, v in dep.items()):
            continue
        out[key] = q
    return out


def _flat(state: dict) -> dict:
    """Laya answers are nested {choice|score|noul, confidence}; depends_on compares scalars."""
    out = {}
    for k, v in state.items():
        if isinstance(v, dict):
            for f in ("choice", "noul", "score"):
                if f in v:
                    out[k] = v[f]
                    break
            else:
                out[k] = v
        else:
            out[k] = v
    return out


def _score_index(answers: dict, key: str, default: int = 0) -> int:
    """Score answers are 0-based expected values (float); round + clamp to criteria index."""
    a = answers.get(key) or {}
    val = a.get("score") if isinstance(a, dict) else a
    if val is None:
        return default
    try:
        return max(0, int(round(float(val))))
    except Exception:
        return default


def _app_hint(transcript: str) -> str | None:
    """'open/launch/start <known-app>' → app name. Models often route this to media."""
    import re
    m = re.search(
        r"\b(?:open|launch|start|run)\s+(?:the\s+|my\s+)?(\w+)",
        transcript.lower(),
    )
    if m and m.group(1) in APPS:
        return m.group(1)
    return None


def decide(router: Router, transcript: str, gate: float = GATE) -> dict:
    """Run Laya → returns {answers, task, confidence, actions, needs_llm}."""
    answers: dict = {}
    # Iteratively resolve: predict only unanswered active questions until stable
    for _ in range(4):
        qs = active_questions(answers)
        qs = {qid: q for qid, q in qs.items() if qid not in answers}
        if not qs:
            break
        result = router.predict({"transcript": transcript}, qs) or {}
        new = result.get("answers") or {}
        if not new:
            break
        answers.update(new)

    task = _extract_choice(answers, "task")
    task = task.strip().lower() if isinstance(task, str) else "none"
    conf = _extract_conf(answers, "task")

    # Regex hint: open/launch + known app name — trust the chain even if task conf is low
    hint = _app_hint(transcript)
    if hint:
        task = "app"
        conf = max(conf, 0.9)
        answers.setdefault("app_target", {"choice": hint, "answer_confidence": 0.9})
        answers.setdefault("app_action", {"choice": "open", "answer_confidence": 0.9})

    actions = split_actions(answers, task, conf, gate)
    needs_llm = conf < gate or task == "none" or not actions
    return {
        "answers": answers,
        "task": task,
        "confidence": conf,
        "actions": actions,
        "needs_llm": needs_llm,
    }


def _extract_choice(answers: dict, key: str):
    a = answers.get(key) or {}
    if "choice" in a:
        return a["choice"]
    if "noul" in a:
        return a["noul"]
    if "score" in a:
        return a["score"]
    return None


def _extract_conf(answers: dict, key: str) -> float:
    a = answers.get(key) or {}
    if not isinstance(a, dict):
        return 0.0
    v = a.get("answer_confidence")
    if v is None:
        v = a.get("confidence") or 0.0
    return float(v)


def split_actions(answers: dict, task: str, conf: float, gate: float) -> list[dict]:
    """Convert Laya answers → list of {action, ...} dicts for the dispatcher."""
    if conf < gate or task in ("none", None):
        return []

    def ch(key):
        return _extract_choice(answers, key)

    if task == "app":
        target = ch("app_target") or "spotify"
        act = ch("app_action") or "open"
        return [{"action": "app", "mode": act, "app": APPS.get(target, target)}]
    if task == "volume":
        direction = ch("volume_direction") or "up"
        if direction == "set":
            idx = _score_index(answers, "volume_level", default=2)
            pct = VOL[idx] if 0 <= idx < len(VOL) else 50
            return [{"action": "volume_set", "pct": pct}]
        if direction == "mute":
            return [{"action": "volume_mute"}]
        step = 10 if direction == "up" else -10
        return [{"action": "volume_step", "step": step}]
    if task == "display":
        kind = ch("display_kind") or "brightness"
        direction = ch("display_direction") or "up"
        if kind == "dark":
            return [{"action": "dark_mode", "on": direction in ("on", "up")}]
        step = 10 if direction in ("up", "on") else -10
        return [{"action": "brightness", "step": step}]
    if task == "media":
        return [{"action": "media", "cmd": ch("media_action") or "play"}]
    if task == "system":
        return [{"action": "system", "cmd": ch("system_action") or "lock"}]
    if task == "timer":
        idx = _score_index(answers, "timer_minutes", default=2)
        minutes = TIMER_MIN[idx] if 0 <= idx < len(TIMER_MIN) else 5
        return [{"action": "timer", "minutes": minutes}]
    return []


# --- Self-check (no GPU/LLM needed) ------------------------------------------
def demo():
    """Fixture-based smoke test: mock router answers → split_actions."""
    cases = [
        (
            {"task": {"choice": "volume", "confidence": 0.9},
             "volume_direction": {"choice": "up", "confidence": 0.9}},
            "volume_step",
        ),
        (
            {"task": {"choice": "media", "confidence": 0.88},
             "media_action": {"choice": "pause", "confidence": 0.95}},
            "media",
        ),
        (
            {"task": {"choice": "app", "confidence": 0.9},
             "app_action": {"choice": "open", "confidence": 0.9},
             "app_target": {"choice": "spotify", "confidence": 0.9}},
            "app",
        ),
        (
            {"task": {"choice": "none", "confidence": 0.4}},
            None,  # needs_llm
        ),
        (
            {"task": {"choice": "timer", "confidence": 0.85},
             "timer_minutes": {"score": 3, "confidence": 0.8}},
            "timer",
        ),
    ]
    for answers, expect in cases:
        task = _extract_choice(answers, "task") or "none"
        conf = _extract_conf(answers, "task")
        acts = split_actions(answers, task, conf, GATE)
        if expect is None:
            assert not acts, f"expected no actions for {answers}, got {acts}"
        else:
            assert acts and acts[0]["action"] == expect, f"{answers} → {acts}"
    # active_questions filtering
    qs = active_questions({"task": "volume"})
    assert "volume_direction" in qs and "media_action" not in qs
    qs2 = active_questions({"task": {"choice": "media", "confidence": 0.9}})
    assert "media_action" in qs2 and "volume_direction" not in qs2
    print("decide.demo OK —", len(cases), "cases")


if __name__ == "__main__":
    demo()
