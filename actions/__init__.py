"""Action dispatcher — routes decide() actions to platform modules."""
from __future__ import annotations

import importlib
import sys

from replies import reply

_mod = "windows" if sys.platform.startswith("win") else "linux"
plat = importlib.import_module(f"actions.{_mod}")


def run(action: dict) -> str:
    """Execute one action dict; return a reply key or spoken string."""
    kind = action.get("action")

    if kind == "app":
        mode = action.get("mode", "open")
        ok = plat.open_app(action.get("app", ""))
        if not ok:
            return reply("app.fail")
        return reply("app.focus") if mode == "focus" else reply("app.open")

    if kind == "url":
        ok = plat.open_url(action.get("url", ""))
        custom_reply = action.get("reply_key")
        if custom_reply:
            return reply(custom_reply) if ok else reply("error")
        return reply("app.open") if ok else reply("error")

    if kind == "search":
        query = action.get("query", "")
        engine = action.get("engine", "google")
        ok = plat.search_web(query, engine=engine)
        if engine == "youtube":
            return reply("web.search_youtube") if ok else reply("error")
        return reply("web.search") if ok else reply("error")

    if kind == "screenshot":
        ok = plat.take_screenshot()
        return reply("screenshot.done") if ok else reply("screenshot.fail")

    if kind == "time":
        return plat.get_current_time()

    if kind == "date":
        return plat.get_current_date()

    if kind == "volume_step":
        ok = plat.volume_step(action.get("step", 10))
        return reply("volume.set") if ok else reply("volume.fail")

    if kind == "volume_set":
        ok = plat.volume_set(action.get("pct", 50))
        return reply("volume.set") if ok else reply("volume.fail")

    if kind == "volume_mute":
        state = plat.volume_mute()
        if state == "unmuted":
            return reply("volume.unmute")
        if state in ("muted", True):
            return reply("volume.mute")
        return reply("volume.fail")

    if kind == "brightness":
        ok = plat.brightness(action.get("step", 10))
        return reply("brightness.up" if action.get("step", 0) > 0 else "brightness.down") if ok else reply("error")

    if kind == "dark_mode":
        on = bool(action.get("on", True))
        ok = plat.dark_mode(on)
        return reply("dark.on" if on else "dark.off") if ok else reply("error")

    if kind == "media":
        cmd = action.get("cmd", "play")
        ok = plat.media(cmd)
        if not ok:
            return reply("media.none")
        return reply(f"media.{cmd}")

    if kind == "system":
        cmd = action.get("cmd", "lock")
        force = bool(action.get("force", False))
        if cmd in ("shutdown", "reboot") and not force:
            # Protected by default
            return reply("system.confirm")
        ok = plat.system(cmd, force=force)
        return reply(f"system.{cmd}") if ok else reply("error")

    if kind == "timer":
        # Handled by main via TimerBank — return key only
        return "timer.set"

    return reply("error")

