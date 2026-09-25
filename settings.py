"""Settings manager for Hey Laya — JSON configuration + hotkey + mic selection."""
from __future__ import annotations

import json
from pathlib import Path

SETTINGS_FILE = Path.home() / ".config" / "hey-laya" / "settings.json"
# Local fallback
LOCAL_SETTINGS = Path(__file__).parent / "settings.json"

DEFAULT_SETTINGS = {
    "hotkey": "alt_r",
    "mode": "both",  # both | ptt | wake
    "stt_model": "base",
    "wake_model": "tiny",
    "mic_device": None,  # int or None for default
    "always_on_top": True,
    "safe_shutdown": True,
    "window_pos": None,  # [x, y]
    # LLM backend: "local" (llama-server/ollama autodetect) or "api"
    # (OpenAI-compatible endpoint with key).
    "llm_provider": "local",
    "llm_api_url": "",
    "llm_api_key": "",
    "llm_api_model": "gpt-4o-mini",
    # Global hotkey that shows/focuses the app window, e.g. "ctrl+alt+l".
    # (Wayland/X11 without focus: pynput only sees keys while a window is
    # focused — the in-window Space hold-to-talk always works.)
    "launch_hotkey": "ctrl+alt+l",
    # Compact always-on-top widget pinned to the top-right corner.
    "mini_widget": True,
}


def get_settings_path() -> Path:
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        return SETTINGS_FILE
    except Exception:
        return LOCAL_SETTINGS


def load_settings() -> dict:
    p = get_settings_path()
    if p.exists():
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            merged = dict(DEFAULT_SETTINGS)
            merged.update(data)
            return merged
        except Exception:
            pass
    return dict(DEFAULT_SETTINGS)


def save_settings(settings: dict) -> bool:
    p = get_settings_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
        return True
    except Exception as e:
        print(f"[settings] save failed: {e}")
        return False
