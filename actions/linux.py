"""Linux system actions — playerctl / wpctl / gsettings / xdg-open / browser / tools."""
from __future__ import annotations

import datetime
import shutil
import subprocess
import urllib.parse
import webbrowser
from pathlib import Path


def _run(cmd: list[str], timeout: float = 5.0) -> bool:
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return r.returncode == 0
    except Exception:
        return False


def open_url(url: str) -> bool:
    """Open a web URL with default browser or xdg-open."""
    if not url:
        return False
    try:
        if webbrowser.open(url):
            return True
    except Exception:
        pass
    return _run(["xdg-open", url])


def search_web(query: str, engine: str = "google") -> bool:
    """Perform a web search in the browser."""
    q = urllib.parse.quote_plus(query.strip())
    if engine == "youtube":
        url = f"https://www.youtube.com/results?search_query={q}"
    elif engine == "wikipedia":
        url = f"https://en.wikipedia.org/wiki/Special:Search?search={q}"
    else:
        url = f"https://www.google.com/search?q={q}"
    return open_url(url)


def take_screenshot() -> bool:
    """Capture a screenshot to Pictures/Screenshots."""
    out_dir = Path.home() / "Pictures" / "Screenshots"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    out_file = out_dir / f"screenshot_{ts}.png"

    # Spectacle (KDE Plasma)
    if shutil.which("spectacle"):
        if _run(["spectacle", "-b", "-n", "-o", str(out_file)]):
            return True
    # grim (generic Wayland)
    if shutil.which("grim"):
        if _run(["grim", str(out_file)]):
            return True
    # gnome-screenshot
    if shutil.which("gnome-screenshot"):
        if _run(["gnome-screenshot", "-f", str(out_file)]):
            return True
    # import (ImageMagick)
    if shutil.which("import"):
        if _run(["import", "-window", "root", str(out_file)]):
            return True
    return False


def get_current_time() -> str:
    now = datetime.datetime.now()
    return f"It's {now.strftime('%-I:%M %p')}."


def get_current_date() -> str:
    now = datetime.datetime.now()
    return f"Today is {now.strftime('%A, %B %-d, %Y')}."


def open_app(app: str) -> bool:
    if not app:
        return False
    if app.startswith("http://") or app.startswith("https://"):
        return open_url(app)
    if app == "browser":
        return _open_browser()
    # Try desktop file / command name directly
    if _run(["xdg-open", app]) or _run(["gtk-launch", app]):
        return True
    # Common aliases
    aliases = {
        "google-chrome": ["google-chrome", "google-chrome-stable", "chromium"],
        "org.gnome.Terminal": ["gnome-terminal", "konsole", "kgx", "alacritty", "kitty", "xterm"],
        "code": ["code", "code-oss"],
        "nautilus": ["dolphin", "nautilus", "nemo", "thunar", "files"],
        "calculator": ["kcalc", "gnome-calculator", "calculator"],
        "settings": ["systemsettings", "gnome-control-center"],
        "spotify": ["spotify"],
        "slack": ["slack"],
        "discord": ["discord", "web.discordapp.com"],
    }
    for name in aliases.get(app, [app]):
        if shutil.which(name):
            return _run([name])
    return False


def _open_browser() -> bool:
    """'open browser' → system default web browser, else first available."""
    candidates: list[str] = []
    try:
        r = subprocess.run(
            ["xdg-settings", "get", "default-web-browser"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        desktop = r.stdout.strip().removesuffix(".desktop")
        if desktop:
            candidates.append(desktop)
    except Exception:
        pass
    candidates += ["firefox", "google-chrome", "chromium", "brave-browser", "microsoft-edge"]
    for name in candidates:
        if shutil.which(name):
            return _run([name])
    try:
        return webbrowser.open("about:blank")
    except Exception:
        return False



def volume_step(step: int) -> bool:
    # wpctl (PipeWire) preferred, pactl fallback
    if shutil.which("wpctl"):
        if step > 0:
            return _run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{abs(step)/100}+"])
        return _run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{abs(step)/100}-"])
    if shutil.which("pactl"):
        sign = "+" if step >= 0 else "-"
        return _run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{sign}{abs(step)}%"])
    return False


def volume_set(pct: int) -> bool:
    pct = max(0, min(150, int(pct)))
    if shutil.which("wpctl"):
        return _run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", str(pct / 100)])
    if shutil.which("pactl"):
        return _run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{pct}%"])
    return False


def volume_mute() -> str | None:
    """Toggle mute; return new state ('muted'|'unmuted') or None on failure."""
    def _state() -> str | None:
        if shutil.which("wpctl"):
            r = subprocess.run(
                ["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and "Volume:" in r.stdout:
                return "muted" if "[MUTED]" in r.stdout else "unmuted"
        return None

    before = _state()
    ok = False
    if shutil.which("wpctl"):
        ok = _run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"])
    elif shutil.which("pactl"):
        ok = _run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"])
    if not ok:
        return None
    after = _state()
    return after if after else ("unmuted" if before == "muted" else "muted")


_xrandr_bright = 1.0  # ponytail: session-local level; xrandr can't query current


def brightness(step: int) -> bool:
    global _xrandr_bright
    if shutil.which("brightnessctl"):
        if step >= 0:
            return _run(["brightnessctl", "set", f"+{step}%"])
        return _run(["brightnessctl", "set", f"{step}%"])
    # GNOME fallback
    if _run(
        [
            "gdbus", "call", "--session",
            "--dest", "org.gnome.SettingsDaemon.Power",
            "--object-path", "/org/gnome/SettingsDaemon/Power",
            "--method", "org.gnome.SettingsDaemon.Power.Screen.Brightness",
            str(max(0, min(100, step))),
        ]
    ):
        return True
    # xrandr gamma (Xwayland / external monitors)
    if shutil.which("xrandr"):
        _xrandr_bright = max(0.2, min(1.0, _xrandr_bright + step / 100.0))
        try:
            r = subprocess.run(
                ["xrandr", "--listmonitors"], capture_output=True, text=True, timeout=3
            )
            out = None
            for line in r.stdout.splitlines():
                if "+*" in line:
                    out = line.split()[-1]
                    break
            if out:
                return _run(
                    ["xrandr", "--output", out, "--brightness", f"{_xrandr_bright:.3f}"]
                )
        except Exception:
            pass
    return False


def dark_mode(on: bool) -> bool:
    val = "prefer-dark" if on else "prefer-light"
    ok = _run(["gsettings", "set", "org.gnome.desktop.interface", "color-scheme", val])
    if not ok:
        ok = _run(["gsettings", "set", "org.gnome.desktop.interface", "gtk-theme", "Yaru-dark" if on else "Yaru"])
    return ok


def media(cmd: str) -> bool:
    if not shutil.which("playerctl"):
        return False
    mapping = {
        "play": "play",
        "pause": "pause",
        "next": "next",
        "previous": "previous",
        "stop": "stop",
    }
    return _run(["playerctl", mapping.get(cmd, "play")])


def system(cmd: str, force: bool = False) -> bool:
    if cmd == "lock":
        for c in (
            ["loginctl", "lock-session"],
            ["gnome-screensaver-command", "-l"],
            ["xdg-screensaver", "lock"],
        ):
            if shutil.which(c[0]) and _run(c):
                return True
        return False
    if cmd == "logout":
        return _run(["loginctl", "terminate-user", "self"])
    if cmd == "shutdown":
        if not force:
            return False  # Protected: must pass through confirmation
        return _run(["systemctl", "poweroff"])
    if cmd == "reboot":
        if not force:
            return False  # Protected: must pass through confirmation
        return _run(["systemctl", "reboot"])
    if cmd == "sleep":
        return _run(["systemctl", "suspend"])
    return False
