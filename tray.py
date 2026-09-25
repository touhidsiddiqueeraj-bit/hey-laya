"""System tray icon for Hey Laya.

Uses pystray when available; otherwise a silent no-op (one log line).
All menu actions only flip queued flags on the UI — the main thread applies
them in tick(), so tray callbacks are thread-safe by construction.
"""
from __future__ import annotations

import threading


def _orb_image(size: int = 64):
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = size // 2
    r = size * 30 // 64
    d.ellipse([c - r, c - r, c + r, c + r], fill=(29, 155, 240, 70))
    r = size * 24 // 64
    d.ellipse([c - r, c - r, c + r, c + r], fill=(29, 155, 240, 150))
    r = size * 18 // 64
    d.ellipse([c - r, c - r, c + r, c + r], fill=(29, 155, 240, 255))
    hr = size * 6 // 64
    d.ellipse([c - hr, c - hr - 6, c + hr - 4, c - 6], fill=(255, 255, 255, 230))
    return img


def start_tray(ui):
    """Run the tray icon in a daemon thread.

    Returns a stop() callable, or None when tray deps/host are missing.
    """
    try:
        import pystray
    except Exception as e:
        print(
            f"[tray] unavailable ({e.__class__.__name__}) — skipping tray icon "
            "(pip install pystray pillow python-xlib on X11)",
            flush=True,
        )
        return None

    try:
        icon = pystray.Icon(
            "hey-laya",
            _orb_image(),
            "Hey Laya",
            menu=pystray.Menu(
                pystray.MenuItem("Show / Hide", lambda *_: ui.toggle_visible(), default=True),
                pystray.MenuItem("Settings…", lambda *_: ui.request_settings()),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", lambda *_: ui.request_quit()),
            ),
        )
    except Exception as e:
        print(f"[tray] icon setup failed ({e!r}) — running without tray", flush=True)
        return None

    def _run():
        try:
            icon.run()
        except Exception as e:
            print(f"[tray] icon loop ended ({e!r})", flush=True)

    threading.Thread(target=_run, daemon=True).start()
    print("[tray] icon running", flush=True)
    return icon.stop
