"""Windows system actions — stubs for secondary target (win32 only)."""
from __future__ import annotations

import os
import subprocess
import sys

IS_WIN = sys.platform.startswith("win")


def _run(cmd: list[str], timeout: float = 5.0) -> bool:
    if not IS_WIN:
        return False
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, shell=False)
        return r.returncode == 0
    except Exception:
        return False


def open_app(app: str) -> bool:
    if not IS_WIN or not app:
        return False
    # os.startfile opens files/URLs; for apps use shell start
    aliases = {
        "spotify": "spotify:",
        "chrome": "chrome",
        "firefox": "firefox",
        "vscode": "code",
        "terminal": "wt",
        "files": "explorer",
        "settings": "ms-settings:",
        "calculator": "calculator:",
    }
    target = aliases.get(app, app)
    try:
        if target.endswith(":") or target.startswith("ms-"):
            os.startfile(target)  # type: ignore[attr-defined]
            return True
        return _run(["cmd", "/c", "start", "", target])
    except Exception:
        return False


def volume_step(step: int) -> bool:
    if not IS_WIN:
        return False
    try:
        from ctypes import POINTER, cast

        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume = cast(interface, POINTER(IAudioEndpointVolume))
        cur = volume.GetMasterVolumeLevelScalar()
        new = max(0.0, min(1.0, cur + step / 100.0))
        volume.SetMasterVolumeLevelScalar(new, None)
        return True
    except Exception:
        return False


def volume_set(pct: int) -> bool:
    if not IS_WIN:
        return False
    try:
        from ctypes import POINTER, cast

        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume = cast(interface, POINTER(IAudioEndpointVolume))
        volume.SetMasterVolumeLevelScalar(max(0.0, min(1.0, pct / 100.0)), None)
        return True
    except Exception:
        return False


def volume_mute() -> bool:
    if not IS_WIN:
        return False
    try:
        from ctypes import POINTER, cast

        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume = cast(interface, POINTER(IAudioEndpointVolume))
        volume.SetMute(1, None)
        return True
    except Exception:
        return False


def brightness(step: int) -> bool:
    # Requires monitor DDC/CI tools — stub
    return False


def dark_mode(on: bool) -> bool:
    if not IS_WIN:
        return False
    key = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
    val = 1 if on else 0
    return _run(
        [
            "reg", "add", f"HKCU\\{key}",
            "/v", "AppsUseLightTheme",
            "/t", "REG_DWORD",
            "/d", str(val),
            "/f",
        ]
    )


def media(cmd: str) -> bool:
    if not IS_WIN:
        return False
    # Media key injection via nircmd or SendInput — use PowerShell as no-dep path
    keys = {
        "play": "MediaPlayPause",
        "pause": "MediaPlayPause",
        "next": "MediaNextTrack",
        "previous": "MediaPrevTrack",
        "stop": "MediaStop",
    }
    k = keys.get(cmd, "MediaPlayPause")
    # SendKeys doesn't do media keys — use virtual key codes via user32
    vk = {
        "MediaPlayPause": 0xB3,
        "MediaNextTrack": 0xB0,
        "MediaPrevTrack": 0xB1,
        "MediaStop": 0xB2,
    }[k]
    script = (
        "Add-Type @'\n"
        "using System;\n"
        "using System.Runtime.InteropServices;\n"
        "public class K {\n"
        "  [DllImport(\"user32.dll\")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, int dwExtraInfo);\n"
        "}\n"
        "'@\n"
        f"[K]::keybd_event({vk}, 0, 0, 0); [K]::keybd_event({vk}, 0, 2, 0)"
    )
    return _run(["powershell", "-NoProfile", "-Command", script])


def system(cmd: str) -> bool:
    if not IS_WIN:
        return False
    if cmd == "lock":
        return _run(["rundll32.exe", "user32.dll,LockWorkStation"])
    if cmd == "logout":
        return _run(["shutdown", "/l"])
    if cmd == "shutdown":
        return _run(["shutdown", "/s", "/t", "0"])
    if cmd == "reboot":
        return _run(["shutdown", "/r", "/t", "0"])
    if cmd == "sleep":
        return _run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
    return False
