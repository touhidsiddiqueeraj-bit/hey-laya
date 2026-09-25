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
    # web & search
    "web.youtube": "Opening YouTube.",
    "web.search_youtube": "Searching YouTube.",
    "web.google": "Opening Google.",
    "web.search": "Searching the web.",
    "web.github": "Opening GitHub.",
    "web.reddit": "Opening Reddit.",
    "web.twitter": "Opening Twitter.",
    "web.gmail": "Opening Gmail.",
    "web.netflix": "Opening Netflix.",
    "web.chatgpt": "Opening ChatGPT.",
    "web.maps": "Opening Google Maps.",
    "web.wikipedia": "Opening Wikipedia.",
    # tools & utility
    "screenshot.done": "Screenshot taken.",
    "screenshot.fail": "Couldn't capture screenshot.",
    "time.current": "Checking the time.",
    "date.current": "Checking the date.",
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
    "system.confirm": "Please confirm: say confirm shutdown to proceed.",
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
    "cancel": "Cancelled.",
}


def reply(key: str) -> str:
    return REPLIES.get(key, REPLIES["error"])

