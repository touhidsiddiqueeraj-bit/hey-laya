"""Laya decision layer — ported from hey-jev QUESTIONS + decide/split_actions."""
from __future__ import annotations

import re
from laya import Router

# Decision gate: answers below this confidence fall through to LLM.
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
    "calculator": "calculator",
    "settings": "settings",
    "browser": "browser",  # resolved to default web browser in actions.linux
    "discord": "discord",
}

# Web URL targets
URLS = {
    "youtube": ("https://www.youtube.com", "web.youtube"),
    "google": ("https://www.google.com", "web.google"),
    "github": ("https://www.github.com", "web.github"),
    "reddit": ("https://www.reddit.com", "web.reddit"),
    "twitter": ("https://www.twitter.com", "web.twitter"),
    "x": ("https://www.x.com", "web.twitter"),
    "gmail": ("https://mail.google.com", "web.gmail"),
    "netflix": ("https://www.netflix.com", "web.netflix"),
    "chatgpt": ("https://chatgpt.com", "web.chatgpt"),
    "maps": ("https://maps.google.com", "web.maps"),
    "wikipedia": ("https://www.wikipedia.org", "web.wikipedia"),
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
            "system": "explicitly lock, logout, shutdown, reboot, or sleep the computer",
            "timer": "set a timer or countdown alarm",
            "none": "not a command — general conversation, conversational stop, or question",
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
            "shutdown": "explicitly power off or shut down the computer",
            "reboot": "explicitly reboot or restart the computer",
            "sleep": "suspend or sleep the computer",
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


def _minutes_from(transcript: str) -> float | None:
    """Parse timer minutes straight from the transcript (Laya's buckets are unreliable)."""
    import re

    t = transcript.lower()
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:minutes?|mins?)\b", t)
    if m:
        return float(m.group(1))
    words = {
        "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
        "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
        "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
        "forty-five": 45, "sixty": 60,
    }
    if re.search(r"\b(?:half\s+an?\s+hour|half\s+hour)\b", t):
        return 30.0
    if re.search(r"\ban?\s+hour\b|\bhours?\b", t):
        return 60.0
    m = re.search(
        r"\b(" + "|".join(words) + r")\s+(?:minutes?|mins?)\b", t,
    )
    if m and words[m.group(1)]:
        return float(words[m.group(1)])
    return None


def fast_match_action(transcript: str) -> list[dict] | None:
    """Zero-latency regex pattern matching for 50+ common commands."""
    t = transcript.lower().strip()
    if not t:
        return None

    # Safety check: Conversational words like "shut up", "stop", "exit" must NEVER shut down PC!
    if re.fullmatch(r"(?:shut\s*up|stop|be\s+quiet|stop\s+talking|cancel|never\s*mind)", t):
        return []  # Treat as conversational stop / none

    # Web search on YouTube: e.g. "search youtube for lo fi music", "youtube search lofi"
    m_yt_search = re.search(r"\b(?:search\s+youtube\s+for|search\s+on\s+youtube\s+for|youtube\s+search)\s+(.+)", t)
    if m_yt_search:
        return [{"action": "search", "engine": "youtube", "query": m_yt_search.group(1).strip()}]

    # General web search: e.g. "search google for weather", "search for how to bake bread"
    m_search = re.search(r"\b(?:search\s+google\s+for|search\s+for|google\s+search|search\s+the\s+web\s+for)\s+(.+)", t)
    if m_search:
        return [{"action": "search", "engine": "google", "query": m_search.group(1).strip()}]
    m_wiki = re.search(r"\b(?:search\s+wikipedia\s+for|wikipedia\s+search)\s+(.+)", t)
    if m_wiki:
        return [{"action": "search", "engine": "wikipedia", "query": m_wiki.group(1).strip()}]

    # URL / Web app destinations
    m_open = re.search(r"\b(?:open|launch|go\s+to|start|visit)\s+(?:the\s+)?([a-z0-9_-]+)", t)
    if m_open:
        target = m_open.group(1)
        if target in URLS:
            url, rkey = URLS[target]
            return [{"action": "url", "url": url, "reply_key": rkey}]
        if target in APPS:
            return [{"action": "app", "mode": "open", "app": APPS[target]}]

    # Screenshot
    if re.search(r"\b(?:take\s+(?:a\s+)?screenshot|capture\s+screen|screenshot)\b", t):
        return [{"action": "screenshot"}]

    # Current Time & Date
    if re.search(r"\b(?:what\s+time\s+is\s+it|what['’]?s\s+the\s+time|current\s+time|tell\s+me\s+the\s+time)\b", t):
        return [{"action": "time"}]
    if re.search(r"\b(?:what\s+is\s+today['’]?s\s+date|what['’]?s\s+the\s+date|today['’]?s\s+date|today\s+date|what\s+day\s+is\s+it)\b", t):
        return [{"action": "date"}]

    # Media controls
    if re.search(r"\b(?:pause(?:\s+music|\s+the\s+music)?|pause\s+playback)\b", t):
        return [{"action": "media", "cmd": "pause"}]
    if re.search(r"\b(?:resume|play(?:\s+music|\s+the\s+music)?|unpause)\b", t):
        return [{"action": "media", "cmd": "play"}]
    if re.search(r"\b(?:next\s+track|next\s+song|skip(?:\s+song)?)\b", t):
        return [{"action": "media", "cmd": "next"}]
    if re.search(r"\b(?:previous\s+track|previous\s+song|prev\s+song|go\s+back)\b", t):
        return [{"action": "media", "cmd": "previous"}]
    if re.search(r"\b(?:stop\s+music|stop\s+the\s+music|stop\s+playback|stop\s+song)\b", t):
        return [{"action": "media", "cmd": "stop"}]

    # Volume controls
    if re.search(r"\b(?:volume\s+up|turn\s+it\s+up|louder|increase\s+volume)\b", t):
        return [{"action": "volume_step", "step": 10}]
    if re.search(r"\b(?:volume\s+down|turn\s+it\s+down|quieter|decrease\s+volume|lower\s+volume)\b", t):
        return [{"action": "volume_step", "step": -10}]
    if re.search(r"\b(?:mute|mute\s+(?:audio|sound|volume)|unmute)\b", t):
        return [{"action": "volume_mute"}]
    m_vol = re.search(r"\b(?:set\s+volume\s+to|volume)\s+(\d{1,3})(?:\s*%)?\b", t)
    if m_vol:
        pct = int(m_vol.group(1))
        return [{"action": "volume_set", "pct": pct}]

    # Display / Theme
    if re.search(r"\b(?:dark\s+mode\s+on|switch\s+to\s+dark\s+mode|enable\s+dark\s+mode)\b", t):
        return [{"action": "dark_mode", "on": True}]
    if re.search(r"\b(?:dark\s+mode\s+off|light\s+mode|switch\s+to\s+light\s+mode|enable\s+light\s+mode)\b", t):
        return [{"action": "dark_mode", "on": False}]
    if re.search(r"\b(?:brightness\s+up|increase\s+brightness|screen\s+brighter)\b", t):
        return [{"action": "brightness", "step": 10}]
    if re.search(r"\b(?:brightness\s+down|decrease\s+brightness|screen\s+dimmer|dim\s+screen)\b", t):
        return [{"action": "brightness", "step": -10}]

    # System lock / sleep
    if re.search(r"\b(?:lock\s+(?:the\s+)?computer|lock\s+screen|lock\s+session|lock\s+pc)\b", t):
        return [{"action": "system", "cmd": "lock"}]
    if re.search(r"\b(?:sleep\s+(?:the\s+)?computer|suspend\s+(?:pc|computer)|put\s+pc\s+to\s+sleep)\b", t):
        return [{"action": "system", "cmd": "sleep"}]
    if re.search(r"\b(?:log\s*out|sign\s*out)\b", t):
        return [{"action": "system", "cmd": "logout"}]

    # Explicit Shutdown / Reboot safety
    if re.search(r"\bconfirm\s+(?:shut\s*down|power\s*off)\b", t):
        return [{"action": "system", "cmd": "shutdown", "force": True}]
    if re.search(r"\bconfirm\s+(?:reboot|restart)\b", t):
        return [{"action": "system", "cmd": "reboot", "force": True}]
    if re.search(r"\b(?:shut\s*down|power\s*off)\s+(?:the\s+)?(?:pc|computer|system)\b", t):
        return [{"action": "system", "cmd": "shutdown", "force": False}]
    if re.search(r"\b(?:reboot|restart)\s+(?:the\s+)?(?:pc|computer|system)\b", t):
        return [{"action": "system", "cmd": "reboot", "force": False}]

    # Timer direct parse
    if re.search(r"\b(?:set\s+a\s+timer|set\s+timer|timer\s+for)\b", t):
        mins = _minutes_from(t)
        return [{"action": "timer", "minutes": mins or 5}]

    return None


def decide(router: Router, transcript: str, gate: float = GATE) -> dict:
    """Run fast match → then Laya if needed → returns {answers, task, confidence, actions, needs_llm}."""
    fast_acts = fast_match_action(transcript)
    if fast_acts is not None:
        if not fast_acts:
            return {
                "answers": {},
                "task": "none",
                "confidence": 0.95,
                "actions": [],
                "needs_llm": True,
            }
        first_act = fast_acts[0].get("action", "")
        task_map = {
            "url": "app", "app": "app", "search": "app", "screenshot": "app",
            "time": "app", "date": "app", "volume_step": "volume", "volume_set": "volume",
            "volume_mute": "volume", "dark_mode": "display", "brightness": "display",
            "media": "media", "system": "system", "timer": "timer",
        }
        return {
            "answers": {},
            "task": task_map.get(first_act, "app"),
            "confidence": 0.99,
            "actions": fast_acts,
            "needs_llm": False,
        }

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

    actions = split_actions(answers, task, conf, gate, transcript=transcript)
    # Laya's timer_minutes buckets often pick the wrong one — trust the transcript
    if actions and actions[0].get("action") == "timer":
        mins = _minutes_from(transcript)
        if mins:
            actions[0]["minutes"] = mins
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


def split_actions(answers: dict, task: str, conf: float, gate: float, transcript: str = "") -> list[dict]:
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
        sys_cmd = ch("system_action") or "lock"
        # Safety check: shutdown/reboot requires explicit words in transcript
        t_low = transcript.lower()
        if sys_cmd in ("shutdown", "reboot"):
            if not any(w in t_low for w in ("shut down", "shutdown", "power off", "turn off the", "reboot", "restart")):
                # Spurious match (e.g. from conversational stop/quit/exit)
                return []
        return [{"action": "system", "cmd": sys_cmd, "force": False}]
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
