"""Canned replies for actions and LLM-miss fallbacks. Warm-cache these at startup."""
from __future__ import annotations

REPLIES = {
    # media
    "media.play": "Playing.",
    "media.pause": "Paused.",
    "media.next": "Next track.",
    "media.previous": "Previous track.",
    "media.stop": "Stopped.",
    "media.none": "Nothing is playing right now.",
    # volume
    "volume.up": "Turning it up.",
    "volume.down": "Turning it down.",
    "volume.mute": "Muted.",
    "volume.unmute": "Unmuted.",
    "volume.set": "Volume set.",
    "volume.fail": "Couldn't change system volume.",
    # app
    "app.open": "Opening it.",
    "app.focus": "Bringing it forward.",
    "app.fail": "Couldn't open that app.",
    # display
    "brightness.up": "Brightness up.",
    "brightness.down": "Brightness down.",
    "dark.on": "Dark mode on.",
    "dark.off": "Dark mode off.",
    # system
    "system.lock": "Locking now.",
    "system.logout": "Logging out.",
    "system.shutdown": "Shutting down.",
    "system.reboot": "Rebooting.",
    "system.sleep": "Going to sleep.",
    "system.confirm": "Are you sure? Say yes to confirm.",
    # timer
    "timer.set": "Timer started.",
    "timer.done": "Time's up.",
    "timer.fail": "Couldn't set that timer.",
    # misc
    "greet": "Hey. I'm listening.",
    "cant_answer": "I can't answer that yet — no local model is running.",
    "error": "Something went wrong.",
    "listening": "Listening.",
    "thinking": "One moment.",
}


def reply(key: str) -> str:
    return REPLIES.get(key, REPLIES["error"])
