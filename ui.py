"""Hey Laya orb UI — high-end animated companion (see UI mockup).

States mirror the mockup: Ready=blue, Listening=green, Working=magenta,
Failed=red; an idle active timer shows the big amber countdown ring, and any
speech task temporarily takes over with normal animations, then hands back.
All setters are thread-safe (queued, applied in tick() on the main thread).
"""
from __future__ import annotations

import math
import threading
import time
import tkinter as tk
from collections import deque
from tkinter import ttk

from settings import load_settings, save_settings

BG = "#f4f6fa"
CARD = "#ffffff"
INK = "#232838"
MUTED = "#8b93a7"
LINE = "#e3e7ef"
MINI_BG = "#0d0f16"  # mini widget key color: vanishes under -transparentcolor,
# and reads as a deliberate dark widget where transparency is refused

WIN_W, WIN_H = 340, 545

COLORS = {
    "ready": {"core": "#1d9bf0", "edge": "#0b7fd4", "label": "Ready"},
    "listen": {"core": "#22c55e", "edge": "#149a44", "label": "Listening"},
    "exec": {"core": "#e843b3", "edge": "#b81f88", "label": "Working"},
    "error": {"core": "#f03333", "edge": "#b81f1f", "label": "Failed"},
    "loading": {"core": "#6366f1", "edge": "#4338ca", "label": "Loading…"},
}
TIMER_RING = "#f5a623"
TIMER_TRACK = "#eee0c2"
TIMER_TEXT = "#b97a0c"

MAIN_DT = 0.05  # main orb redraw interval when busy (breathing is fine slower)
MINI_DT = 0.12  # mini widget redraw interval when busy
IDLE_MAIN_DT = 0.12  # ready + no timer: breathing doesn't need 20fps
IDLE_MINI_DT = 0.25
PUMP_CAP = 0.05  # draws+update run at most 20x/s no matter how many threads tick

FONT = "Helvetica"


def _pick_font(root: tk.Tk) -> str:
    """Nicest available UI font; falls back gracefully."""
    try:
        import tkinter.font as tkfont

        avail = set(tkfont.families())
        for name in ("Inter", "Segoe UI", "SF Pro Text", "Helvetica Neue", "Helvetica", "DejaVu Sans"):
            if name in avail:
                return name
    except Exception:
        pass
    return "TkDefaultFont"


def _hex_lerp(a: str, b: str, t: float) -> str:
    """Blend two #rrggbb colors (t=0 → a, t=1 → b)."""
    t = min(1.0, max(0.0, t))
    ar, ag, ab = int(a[1:3], 16), int(a[3:5], 16), int(a[5:7], 16)
    br, bg, bb = int(b[1:3], 16), int(b[3:5], 16), int(b[5:7], 16)
    return f"#{round(ar + (br - ar) * t):02x}{round(ag + (bg - ag) * t):02x}{round(ab + (bb - ab) * t):02x}"


def _logo_image(size: int = 32) -> tk.PhotoImage:
    """Glossy orb app icon, rasterized once at startup."""
    img = tk.PhotoImage(width=size, height=size)
    c = (size - 1) / 2
    for y in range(size):
        row = []
        for x in range(size):
            d = math.hypot(x - c, y - c) / (size / 2)
            if d > 1.0:
                continue
            # radial gloss: bright top-left → deep edge
            shade = min(1.0, max(0.0, (d - 0.15) / 0.85))
            col = _hex_lerp("#6cc4f7", "#0b7fd4", shade)
            row.append((x, col))
        for x, col in row:
            img.put(col, (x, y))
    # specular highlight
    hx, hy, hr = int(size * 0.36), int(size * 0.30), max(2, size // 9)
    for y in range(hy - hr, hy + hr):
        for x in range(hx - hr, hx + hr):
            if 0 <= x < size and 0 <= y < size and math.hypot(x - hx, y - hy) <= hr:
                img.put("#e8f5fe", (x, y))
    return img


class StatusUI:
    def __init__(self, on_mode_change=None, on_ptt_start=None, on_ptt_end=None,
                 on_settings_change=None, on_quit=None, on_text=None):
        self.settings = load_settings()
        self.root = tk.Tk()
        global FONT
        FONT = _pick_font(self.root)
        # HiDPI: scale fonts/widgets to the real screen density.
        try:
            density = self.root.winfo_fpixels("1i") / 72.0
            if density > 1.05:
                self.root.tk.call("tk", "scaling", density)
        except Exception:
            pass
        self.root.title("Hey Laya")
        self.root.configure(bg=BG)
        self.root.geometry(f"{WIN_W}x{WIN_H}")
        self.root.resizable(False, False)
        self._icon = _logo_image()
        try:
            self.root.iconphoto(False, self._icon)
        except Exception:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self.request_quit)

        self._on_mode = on_mode_change
        self._on_ptt_start = on_ptt_start
        self._on_ptt_end = on_ptt_end
        self._on_settings_change = on_settings_change
        self._on_text = on_text
        self.on_quit = on_quit

        self._state = "ready"
        self._detail = "Say “Hey Laya” or hold the orb"
        self._pending_state = None
        self._pending_timer: float | None | str = "none"  # "none" = no change
        self._pending_visibility = False
        self._pending_quit = False
        self._pending_settings = False
        self._timer_total = 0.0
        self._timer_ends = 0.0
        self._timer_mode = False  # currently showing the big ring (label cache)
        self._t0 = time.monotonic()
        self._last_draw_main = 0.0
        self._last_draw_mini = 0.0
        self._last_pump = 0.0
        self._pump_lock = threading.Lock()
        self._draw_ema = 0.0  # smoothed main-draw cost; drives auto quality
        self._low_q = False
        self._low_recover = self._t0
        self._pump_count = 0
        self._slow_note = 0.0
        self._err_since = 0.0
        self._is_held = False
        self._hidden = False
        self._history: deque[str] = deque(maxlen=3)
        self.mini = None
        self._mini_drag = {"x": 0, "y": 0, "moved": False}

        self.root.attributes("-topmost", bool(self.settings.get("always_on_top", True)))
        pos = self.settings.get("window_pos")
        if pos and len(pos) == 2:
            self.root.geometry(f"{WIN_W}x{WIN_H}+{pos[0]}+{pos[1]}")

        # Two views in one window: orb stage <-> settings panel.
        self.view_orb = tk.Frame(self.root, bg=BG)
        self.view_orb.pack(fill=tk.BOTH, expand=True)
        self.view_settings = tk.Frame(self.root, bg=BG)
        self._sv: dict = {}
        self._settings_built = False

        # Header: logo mark + wordmark + state pill + gear
        top = tk.Frame(self.view_orb, bg=BG)
        top.pack(fill=tk.X, padx=14, pady=(12, 0))
        self._logo_cv = tk.Canvas(top, width=30, height=30, bg=BG, highlightthickness=0)
        self._logo_cv.pack(side=tk.LEFT)
        self._draw_logo()
        tk.Label(top, text="Hey Laya", font=(FONT, 15, "bold"), fg=INK, bg=BG).pack(side=tk.LEFT, padx=(6, 0))
        self._pill = tk.Label(top, text="● Ready", font=(FONT, 9, "bold"),
                              fg=COLORS["ready"]["core"], bg=BG)
        self._pill.pack(side=tk.LEFT, padx=(8, 0))
        gear = tk.Label(top, text="⚙", font=(FONT, 15), fg=MUTED, bg=BG, cursor="hand2")
        gear.pack(side=tk.RIGHT)
        gear.bind("<Button-1>", lambda e: self.open_settings())

        # Orb canvas
        self.canvas_size = 280
        self.canvas = tk.Canvas(self.view_orb, width=self.canvas_size, height=self.canvas_size,
                                bg=BG, highlightthickness=0, cursor="hand2")
        self.canvas.pack(pady=(0, 0))
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", lambda e: self.open_settings())

        # State + detail
        self.lbl_status = tk.Label(self.view_orb, text="Ready", font=(FONT, 14, "bold"),
                                   fg=COLORS["ready"]["core"], bg=BG)
        self.lbl_status.pack()
        self.lbl_detail = tk.Label(self.view_orb, text=self._detail, font=(FONT, 10),
                                   fg=MUTED, bg=BG, wraplength=300, justify=tk.CENTER)
        self.lbl_detail.pack(pady=(0, 4))

        # Transcript card
        card = tk.Frame(self.view_orb, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        card.pack(fill=tk.X, padx=14, pady=(2, 6))
        tk.Label(card, text="TRANSCRIPT", font=(FONT, 8, "bold"), fg=MUTED, bg=CARD,
                 anchor=tk.W).pack(fill=tk.X, padx=10, pady=(7, 0))
        self.lbl_hist = tk.Label(card, text="—", font=(FONT, 10), fg=INK, bg=CARD,
                                 wraplength=290, justify=tk.LEFT, anchor=tk.W)
        self.lbl_hist.pack(fill=tk.X, padx=10, pady=(2, 9))

        # Footer: type-to-talk row + settings button
        trow = tk.Frame(self.view_orb, bg=BG)
        trow.pack(fill=tk.X, padx=14, pady=(0, 6))
        self._text_var = tk.StringVar()
        self._text_entry = tk.Entry(trow, textvariable=self._text_var, bg=CARD, fg=INK,
                                    font=(FONT, 10), highlightbackground=LINE, highlightthickness=1,
                                    relief=tk.FLAT, insertbackground=INK)
        self._text_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=5)
        self._text_entry.bind("<Return>", lambda e: self._on_text_send())
        tk.Button(trow, text="Send", font=(FONT, 10, "bold"),
                  bg=CARD, fg=INK, activebackground=LINE, activeforeground=INK,
                  relief=tk.FLAT, padx=10, cursor="hand2",
                  highlightbackground=LINE, highlightthickness=1,
                  command=self._on_text_send).pack(side=tk.RIGHT, padx=(6, 0))
        footer = tk.Frame(self.view_orb, bg=BG)
        footer.pack(fill=tk.X, padx=14, pady=(0, 12))
        tk.Button(footer, text="Settings", font=(FONT, 11, "bold"),
                  bg=CARD, fg=INK, activebackground=LINE, activeforeground=INK,
                  relief=tk.FLAT, pady=6, cursor="hand2",
                  highlightbackground=LINE, highlightthickness=1,
                  command=self.open_settings).pack(fill=tk.X)

        self.root.bind("<KeyPress-space>", self._key_down)
        self.root.bind("<KeyRelease-space>", self._key_up)
        self._drag = {"x": 0, "y": 0}
        self.root.bind("<ButtonPress-1>", self._start_drag)
        self.root.bind("<B1-Motion>", self._on_drag)
        self.root.bind("<ButtonRelease-1>", self._end_drag)

        if self.settings.get("mini_widget", True):
            self._create_mini()
        self._draw_main(0.0)

    # --- thread-safe setters (applied in tick) -----------------------------
    def set_state(self, state: str, detail: str = ""):
        """state: ready | listen | exec | error."""
        self._pending_state = (state, detail)

    def set_timer(self, seconds: float | None):
        """Show the amber countdown ring (None clears it)."""
        self._pending_timer = seconds

    def toggle_visible(self):
        self._pending_visibility = True

    def request_quit(self, _event=None):
        self._pending_quit = True

    def request_settings(self):
        self._pending_settings = True

    def mode(self) -> str:
        return self.settings.get("mode", "both")

    # --- input --------------------------------------------------------------
    def _start_drag(self, event):
        if event.widget is not self.canvas:
            self._drag["x"], self._drag["y"] = event.x, event.y

    def _on_drag(self, event):
        if event.widget is not self.canvas:
            x = self.root.winfo_x() + (event.x - self._drag["x"])
            y = self.root.winfo_y() + (event.y - self._drag["y"])
            self.root.geometry(f"+{x}+{y}")

    def _end_drag(self, _event):
        self.settings["window_pos"] = [self.root.winfo_x(), self.root.winfo_y()]
        save_settings(self.settings)

    def _on_press(self, _event=None):
        if not self._is_held:
            self._is_held = True
            if self._on_ptt_start:
                self._on_ptt_start()

    def _on_release(self, _event=None):
        if self._is_held:
            self._is_held = False
            if self._on_ptt_end:
                self._on_ptt_end()

    def _key_down(self, event):
        if isinstance(event.widget, tk.Entry):
            return  # typing in the text box — Space is just a space
        if not (event.state & 0x1):
            self._on_press()

    def _key_up(self, event):
        if isinstance(event.widget, tk.Entry):
            return
        if not (event.state & 0x1):
            self._on_release()

    def _on_text_send(self):
        text = self._text_var.get().strip()
        if not text:
            return
        self._text_var.set("")
        if self._on_text:
            self._on_text(text)

    # --- state application ---------------------------------------------------
    def _apply_state(self, state: str, detail: str):
        self._state = state if state in COLORS else "ready"
        if detail:
            self._detail = detail
            if self._state in ("exec", "error"):
                col = COLORS[self._state]
                self._history.append(f"{col['label']}: {detail[:70]}")
        if self._state == "error":
            self._err_since = time.monotonic()
        self._refresh_labels()

    def _refresh_labels(self):
        """Status/pill/detail labels for the current mode (timer ring or orb)."""
        timer_left = self._timer_left()
        timer_mode = timer_left is not None and self._state == "ready"
        if timer_mode:
            self.lbl_status.config(text="Timer", fg=TIMER_TEXT)
            self._pill.config(text="● Timer", fg=TIMER_TEXT)
        else:
            col = COLORS.get(self._state, COLORS["ready"])
            self.lbl_status.config(text=col["label"], fg=col["core"])
            self._pill.config(text=f"● {col['label']}", fg=col["core"])
        self.lbl_detail.config(text=self._detail[:90])
        self.lbl_hist.config(text="\n".join(self._history) or "—")

    def _apply_timer(self, seconds: float | None):
        if seconds is None or seconds <= 0:
            self._timer_total = 0.0
            self._timer_ends = 0.0
        else:
            self._timer_total = float(seconds)
            self._timer_ends = time.monotonic() + float(seconds)
        self._refresh_labels()

    def _timer_left(self) -> float | None:
        if self._timer_total <= 0:
            return None
        left = self._timer_ends - time.monotonic()
        if left <= 0:
            self._timer_total = 0.0
            return None
        return left

    # --- drawing --------------------------------------------------------------
    def _draw_logo(self):
        cv = self._logo_cv
        cv.delete("all")
        cv.create_oval(2, 2, 28, 28, outline="#c9e7fb", width=1)
        cv.create_oval(4, 4, 26, 26, outline="#7cc7f5", width=1)
        cv.create_oval(7, 7, 23, 23, fill="#1d9bf0", outline="#0b7fd4", width=1)
        cv.create_oval(10, 9, 15, 13, fill="#e8f5fe", outline="")

    def _glow(self, cv: tk.Canvas, cx: float, cy: float, r_core: float,
              r_halo: float, core: str, steps: int = 10, bg: str = BG,
              snap: bool = True):
        """Fake Gaussian glow: concentric rings fading core → background."""
        w = max(1, int((r_halo - r_core) / steps) + 1)
        for i in range(steps, 0, -1):
            f = i / steps
            r = r_core + (r_halo - r_core) * f
            cv.create_oval(cx - r, cy - r, cx + r, cy + r,
                           outline=_hex_lerp(core, bg, f ** 0.65), width=w)
        if not snap:
            return
        # Snap band: exact-background rings past the halo. Invisible on a
        # solid canvas; swallowed by the shape mask on the floating widget —
        # either way no dark smudge fringe survives.
        for j in (1, 2):
            r = r_halo + w * j
            cv.create_oval(cx - r, cy - r, cx + r, cy + r, outline=bg, width=w)

    def _core(self, cv: tk.Canvas, cx: float, cy: float, r: float, state: str, t: float = 0.0):
        col = COLORS[state]
        cv.create_oval(cx - r, cy - r, cx + r, cy + r,
                       fill=col["core"], outline=col["edge"], width=2)
        # specular highlight slowly orbits so she feels alive
        hx = cx + math.cos(t * 0.7) * r * 0.14
        hy = cy - r * 0.24 + math.sin(t * 0.7) * r * 0.14
        hr = r * 0.30
        cv.create_oval(hx - hr, hy - hr * 0.7, hx + hr * 0.5, hy + hr * 0.4,
                       fill="#ffffff", outline="")

    @staticmethod
    def _blink(t: float) -> float:
        """Rare slow blink: scale factor dipping to ~0.9 every ~7s."""
        ph = t % 7.0
        return 1.0 - 0.10 * math.exp(-((ph - 2.0) ** 2) / 0.004)

    @staticmethod
    def _heartbeat(t: float) -> float:
        """Double-thump pulse for the listening state."""
        th = t % 1.2
        return (0.05 * math.exp(-((th - 0.20) ** 2) / 0.004)
                + 0.035 * math.exp(-((th - 0.42) ** 2) / 0.004))

    def _draw_orb(self, cv: tk.Canvas, size: int, t: float, timer_suppress: bool = False,
                  bg: str = BG, light: bool = False):
        """Shared orb renderer for the main canvas and the mini widget."""
        cv.delete("all")
        cx = cy = size // 2
        R = size * 0.145  # core radius scales with canvas
        col = COLORS.get(self._state, COLORS["ready"])
        timer_left = self._timer_left()
        steps = 4 if light else 10
        ripples = 1 if light else 3
        t_hl = 0.0 if light else t  # frozen highlight in light mode

        # Timer ring owns the stage only when idle; any live task takes over
        # with normal animations and the ring returns afterwards.
        if timer_left is not None and self._state == "ready" and not timer_suppress:
            frac = min(1.0, timer_left / self._timer_total) if self._timer_total else 0.0
            RR = size * 0.30
            cv.create_oval(cx - RR, cy - RR, cx + RR, cy + RR, outline=TIMER_TRACK, width=max(8, size // 11))
            if frac > 0:
                cv.create_arc(cx - RR, cy - RR, cx + RR, cy + RR, start=90, extent=-360 * frac,
                              outline=TIMER_RING, width=max(8, size // 11), style=tk.ARC)
            mm, ss = divmod(int(timer_left), 60)
            cv.create_text(cx, cy, text=f"{mm:02d}:{ss:02d}",
                           font=(FONT, max(10, size // 9), "bold"), fill=TIMER_TEXT)
            return "timer"

        shake = 0.0
        if self._state == "error" and time.monotonic() - self._err_since < 0.6:
            shake = size * 0.014 * math.sin(t * 40.0)
        cx += shake
        floaty = math.sin(t * 1.3) * size * 0.006  # gentle hover in every state
        cy += floaty

        if self._state == "ready":
            breathe = (math.sin(t * 2.6) + 1) / 2
            r = (R + size * 0.008 * breathe) * self._blink(t)
            self._glow(cv, cx, cy, r, r + size * 0.11, col["core"],
                       steps=steps, bg=bg, snap=not light)
            self._core(cv, cx, cy, r, "ready", t_hl)
        elif self._state == "listen":
            r = R * (1.0 + self._heartbeat(t)) * self._blink(t)
            for i in range(ripples):  # ripples, staggered
                ph = (t * 0.85 + i / ripples) % 1.0
                rr = r + size * 0.03 + ph * size * 0.20
                cv.create_oval(cx - rr, cy - rr, cx + rr, cy + rr,
                               outline=_hex_lerp(col["core"], bg, ph ** 0.8), width=2)
            self._glow(cv, cx, cy, r, r + size * 0.05, col["core"],
                       steps=min(8, steps), bg=bg, snap=not light)
            self._core(cv, cx, cy, r, "listen", t_hl)
        elif self._state in ("exec", "loading"):
            cy += math.sin(t * 3.1) * size * 0.008  # working bob
            r = R + math.sin(t * 6.0) * size * 0.006
            self._glow(cv, cx, cy, r, r + size * 0.10, col["core"],
                       steps=steps, bg=bg, snap=not light)
            for start, extent, shade, w in (
                ((t * 200) % 360, 100, col["core"], 5),
                ((t * 200 - 45) % 360, 45, _hex_lerp(col["core"], bg, 0.55), 5),
            ):
                cv.create_arc(cx - r - size * 0.035, cy - r - size * 0.035,
                              cx + r + size * 0.035, cy + r + size * 0.035,
                              start=start, extent=extent, outline=shade, width=w, style=tk.ARC)
            self._core(cv, cx, cy, r, self._state, t_hl)
        else:  # error
            self._glow(cv, cx, cy, R, R + size * 0.09, col["core"],
                       steps=steps, bg=bg, snap=not light)
            self._core(cv, cx, cy, R, "error", t_hl)
            cv.create_text(cx, cy, text="!", font=(FONT, max(12, size // 9), "bold"), fill="#ffffff")

        if timer_left is not None:  # busy with a live timer in the background
            mm, ss = divmod(int(timer_left), 60)
            cv.create_text(cx, cy + size * 0.30, text=f"{mm:02d}:{ss:02d}",
                           font=(FONT, max(9, size // 24), "bold"), fill=TIMER_TEXT)
        return "orb"

    def _draw_main(self, t: float, light: bool = False):
        mode = self._draw_orb(self.canvas, self.canvas_size, t, light=light)
        timer_mode = mode == "timer"
        if timer_mode != self._timer_mode:  # transition: swap the caption once
            self._timer_mode = timer_mode
            self._refresh_labels()

    # --- mini widget: just the orb, floating with no background box ------------
    def _create_mini(self):
        if self.mini and self.mini.winfo_exists():
            return
        m = tk.Toplevel(self.root)
        m.overrideredirect(True)
        m.attributes("-topmost", True)
        m.configure(bg=MINI_BG)
        try:
            # 1-bit shape mask on X11/Windows: exact-key pixels vanish, the
            # neon glow fringe stays — a floating orb, no box. Where the WM
            # refuses, MINI_BG reads as a deliberate dark widget (never white).
            m.attributes("-transparentcolor", MINI_BG)
        except Exception:
            pass
        try:
            m.iconphoto(False, self._icon)
        except Exception:
            pass
        sw = m.winfo_screenwidth()
        pos = self.settings.get("mini_pos")
        if pos and len(pos) == 2:
            m.geometry(f"140x140+{pos[0]}+{pos[1]}")
        else:
            m.geometry(f"140x140+{sw - 156}+36")
        self._mini_cv = tk.Canvas(m, width=140, height=140, bg=MINI_BG,
                                 highlightthickness=0, cursor="hand2")
        self._mini_cv.pack()
        m.bind("<ButtonPress-1>", self._mini_press)
        m.bind("<B1-Motion>", self._mini_drag_fn)
        m.bind("<ButtonRelease-1>", self._mini_release)
        m.bind("<Button-3>", lambda e: self.open_settings())
        self.mini = m

    def _destroy_mini(self):
        if self.mini:
            try:
                self.mini.destroy()
            except tk.TclError:
                pass
            self.mini = None

    def _mini_press(self, event):
        self._mini_drag.update(x=event.x, y=event.y, moved=False)

    def _mini_drag_fn(self, event):
        if self.mini is None:
            return
        dx, dy = event.x - self._mini_drag["x"], event.y - self._mini_drag["y"]
        if abs(dx) + abs(dy) > 2:
            self._mini_drag["moved"] = True
        x = self.mini.winfo_x() + dx
        y = self.mini.winfo_y() + dy
        self.mini.geometry(f"+{x}+{y}")

    def _mini_release(self, _event):
        if self.mini is None:
            return
        if not self._mini_drag["moved"]:
            self.toggle_visible()  # click: show/hide the main window
        else:
            self.settings["mini_pos"] = [self.mini.winfo_x(), self.mini.winfo_y()]
            save_settings(self.settings)

    def _draw_mini(self, t: float, light: bool = False):
        if not self.mini or not self.mini.winfo_exists():
            return
        try:
            # Mirror of the main stage (timer ring included), orb only.
            self._draw_orb(self._mini_cv, 140, t, bg=MINI_BG, light=light)
        except tk.TclError:
            pass

    # --- main-thread pump ------------------------------------------------------
    def tick(self):
        if threading.current_thread() is not threading.main_thread():
            return
        t_start = time.monotonic()
        if self._pending_state is not None:
            st, det = self._pending_state
            self._pending_state = None
            self._apply_state(st, det)
        if self._pending_timer != "none":
            secs = self._pending_timer
            self._pending_timer = "none"
            if secs is None or isinstance(secs, float):
                self._apply_timer(secs)
        if self._pending_visibility:
            self._pending_visibility = False
            if self._hidden:
                self.root.deiconify()
                self.root.lift()
                try:
                    self.root.focus_force()
                except Exception:
                    pass
                self._hidden = False
            else:
                self.root.withdraw()
                self._hidden = True
        if self._pending_settings:
            self._pending_settings = False
            self.open_settings()
        if self._pending_quit:
            self._pending_quit = False
            if self.on_quit:
                self.on_quit()
            else:
                self.destroy()
            return
        t = time.monotonic() - self._t0
        # Global pump cap: wake loop, PTT loop, jobs all call tick(), but
        # draws+update run at most 20x/s combined. Extra calls return after
        # applying the cheap pending flags above.
        with self._pump_lock:
            if t - self._last_pump < PUMP_CAP:
                return
            self._last_pump = t
        self._pump_count += 1
        # Ears over eyes: when a single render costs a large fraction of a
        # second (broken software rasterizer), render only every 3rd pump.
        # The skipped pumps cost microseconds, so mic timing, STT and the
        # wake loop keep full speed while the orb becomes a slideshow.
        render = True
        if self._low_q and self._draw_ema > 0.3 and (self._pump_count % 3):
            render = False
        now = time.monotonic()
        busy = (self._state in ("listen", "exec", "loading", "error")
                or self._timer_total > 0)
        if self._low_q:
            dt_main = 0.12 if busy else 0.25
            dt_mini = 0.25 if busy else 0.5
        else:
            dt_main = MAIN_DT if busy else IDLE_MAIN_DT
            dt_mini = MINI_DT if busy else IDLE_MINI_DT
        if render:
            if now - self._last_draw_main >= dt_main:
                self._last_draw_main = now
                t_d = time.monotonic()
                self._draw_main(t, light=self._low_q)
                self._draw_ema += ((time.monotonic() - t_d) - self._draw_ema) * 0.05
            if now - self._last_draw_mini >= dt_mini:
                self._last_draw_mini = now
                self._draw_mini(t, light=self._low_q)
        # Auto quality: a slow software renderer gets light animations
        # (fewer rings, lower rates) instead of burning the CPU.
        if not self._low_q and self._draw_ema > 0.012 and now - self._t0 > 5:
            self._low_q = True
            print("[ui] slow renderer detected — light animations on", flush=True)
        elif self._low_q:
            if self._draw_ema < 0.005:
                if now - self._low_recover > 8:
                    self._low_q = False
                    print("[ui] renderer recovered — full animations on", flush=True)
            else:
                self._low_recover = now
        if render:
            try:
                self.root.update()
            except tk.TclError:
                pass
        dt = time.monotonic() - t_start
        if dt > 0.15 and now - self._slow_note > 30:
            self._slow_note = now
            print(f"[ui] slow tick {dt * 1000:.0f}ms — software renderer? "
                  "animations stay smooth, audio timing is wall-clock safe.", flush=True)

    # --- settings panel: a second view inside the main window -------------------
    def open_settings(self, _event=None):
        """Flip to the in-app settings panel (gear, footer button, tray)."""
        if self._hidden:
            self._pending_visibility = False
            self.root.deiconify()
            try:
                self.root.lift()
            except Exception:
                pass
            self._hidden = False
        self._show_settings()

    def _show_settings(self):
        self._build_settings_once()
        self._refresh_settings_vars()
        self.view_orb.pack_forget()
        self.view_settings.pack(fill=tk.BOTH, expand=True)
        self.root.bind_all("<MouseWheel>", self._set_scroll, add="+")
        self.root.bind_all("<Button-4>", self._set_scroll, add="+")
        self.root.bind_all("<Button-5>", self._set_scroll, add="+")

    def _back_to_orb(self):
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            try:
                self.root.unbind_all(seq)
            except Exception:
                pass
        self.view_settings.pack_forget()
        self.view_orb.pack(fill=tk.BOTH, expand=True)

    def _set_scroll(self, event):
        try:
            if getattr(event, "num", 0) == 4 or getattr(event, "delta", 0) > 0:
                self._set_canvas.yview_scroll(-1, "units")
            elif getattr(event, "num", 0) == 5 or getattr(event, "delta", 0) < 0:
                self._set_canvas.yview_scroll(1, "units")
        except Exception:
            pass

    def _mic_options(self):
        options = ["System Default"]
        mmap: dict[int, int | None] = {0: None}
        try:
            import sounddevice as sd
            for idx, dev in enumerate(sd.query_devices()):
                if dev.get("max_input_channels", 0) > 0:
                    options.append(f"[{idx}] {dev['name']}")
                    mmap[len(options) - 1] = idx
        except Exception:
            pass
        return options, mmap

    def _build_settings_once(self):
        if self._settings_built:
            return
        self._settings_built = True
        sv = self._sv

        head = tk.Frame(self.view_settings, bg=BG)
        head.pack(fill=tk.X, padx=14, pady=(12, 2))
        tk.Button(head, text="← Back", font=(FONT, 10, "bold"), bg=CARD, fg=INK,
                  activebackground=LINE, activeforeground=INK, relief=tk.FLAT,
                  cursor="hand2", highlightbackground=LINE, highlightthickness=1,
                  command=self._back_to_orb).pack(side=tk.LEFT)
        tk.Label(head, text="Settings", font=(FONT, 15, "bold"), fg=INK, bg=BG).pack(side=tk.LEFT, padx=(10, 0))

        self._set_canvas = tk.Canvas(self.view_settings, bg=BG, highlightthickness=0)
        vsb = ttk.Scrollbar(self.view_settings, orient="vertical", command=self._set_canvas.yview)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._set_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(14, 0))
        self._set_canvas.configure(yscrollcommand=vsb.set)
        inner = tk.Frame(self._set_canvas, bg=BG)
        self._set_canvas.create_window((0, 0), window=inner, anchor="nw", tags=("inner",))
        inner.bind("<Configure>",
                   lambda e: self._set_canvas.configure(scrollregion=self._set_canvas.bbox("all")))
        self._set_canvas.bind("<Configure>",
                              lambda e: self._set_canvas.itemconfig("inner", width=e.width))

        def section(title: str, hint: str = ""):
            tk.Label(inner, text=title, font=(FONT, 11, "bold"), fg=INK, bg=BG).pack(anchor=tk.W, pady=(10, 0))
            if hint:
                tk.Label(inner, text=hint, font=(FONT, 8), fg=MUTED, bg=BG).pack(anchor=tk.W)

        def entry(var: tk.StringVar, show: str = ""):
            e = tk.Entry(inner, textvariable=var, show=show, bg=CARD, fg=INK,
                         font=(FONT, 10), highlightbackground=LINE, highlightthickness=1,
                         relief=tk.FLAT, insertbackground=INK)
            e.pack(fill=tk.X, pady=(2, 4), ipady=3)
            return e

        def cap(text: str):
            tk.Label(inner, text=text, font=(FONT, 9), fg=MUTED, bg=BG).pack(anchor=tk.W)

        # 1. Brain (LLM): local model or paste a cloud API key — no JSON needed.
        section("Brain", "Local model is free & private; or paste a cloud API key.")
        sv["prov"] = tk.StringVar(value=self.settings.get("llm_provider", "local"))
        f_prov = tk.Frame(inner, bg=BG)
        f_prov.pack(fill=tk.X, pady=(2, 0))
        for lbl, val in [("Local model", "local"), ("API key (cloud)", "api")]:
            tk.Radiobutton(f_prov, text=lbl, variable=sv["prov"], value=val, font=(FONT, 10),
                           bg=BG, fg=INK, selectcolor=CARD, activebackground=BG).pack(side=tk.LEFT, padx=(0, 16))
        cap("API URL (OpenAI-compatible)")
        sv["url"] = tk.StringVar(value=self.settings.get("llm_api_url", ""))
        entry(sv["url"])
        cap("API key")
        sv["key"] = tk.StringVar(value=self.settings.get("llm_api_key", ""))
        entry(sv["key"], show="•")
        cap("API model")
        sv["model"] = tk.StringVar(value=self.settings.get("llm_api_model", "gpt-4o-mini"))
        entry(sv["model"])

        # 2. Keyboard
        section("Keyboard", "On Wayland, global hotkeys need a focused window.")
        cap("Push-to-talk key")
        sv["hk"] = tk.StringVar(value=self.settings.get("hotkey", "alt_r"))
        ttk.Combobox(inner, textvariable=sv["hk"], state="readonly", font=(FONT, 10),
                     values=["alt_r", "alt_l", "ctrl_space", "f8", "f9", "scroll_lock"]).pack(fill=tk.X, pady=(2, 4))
        cap("Show-app hotkey, e.g. ctrl+alt+l")
        sv["launch"] = tk.StringVar(value=self.settings.get("launch_hotkey", "ctrl+alt+l"))
        entry(sv["launch"])

        # 3. Window
        section("Window")
        sv["pin"] = tk.BooleanVar(value=self.settings.get("always_on_top", True))
        tk.Checkbutton(inner, text="Always on top", variable=sv["pin"], font=(FONT, 10),
                       bg=BG, fg=INK, selectcolor=CARD, activebackground=BG).pack(anchor=tk.W)
        sv["mini"] = tk.BooleanVar(value=self.settings.get("mini_widget", True))
        tk.Checkbutton(inner, text="Mini orb widget in the corner", variable=sv["mini"], font=(FONT, 10),
                       bg=BG, fg=INK, selectcolor=CARD, activebackground=BG).pack(anchor=tk.W)

        # 4. Assistant
        section("Assistant")
        sv["mode"] = tk.StringVar(value=self.settings.get("mode", "both"))
        f_mode = tk.Frame(inner, bg=BG)
        f_mode.pack(fill=tk.X, pady=(2, 0))
        for lbl, val in [("Both", "both"), ("Wake only", "wake"), ("Key only", "ptt")]:
            tk.Radiobutton(f_mode, text=lbl, variable=sv["mode"], value=val, font=(FONT, 10),
                           bg=BG, fg=INK, selectcolor=CARD, activebackground=BG).pack(side=tk.LEFT, padx=(0, 14))
        cap("Microphone")
        sv["mic"] = tk.StringVar(value="System Default")
        sv["mic_combo"] = ttk.Combobox(inner, textvariable=sv["mic"], state="readonly",
                                       font=(FONT, 10), values=["System Default"])
        sv["mic_combo"].pack(fill=tk.X, pady=(2, 4))

        sv["safe"] = tk.BooleanVar(value=self.settings.get("safe_shutdown", True))
        tk.Checkbutton(inner, text="Confirm before PC shutdown / reboot", variable=sv["safe"], font=(FONT, 10),
                       bg=BG, fg=INK, selectcolor=CARD, activebackground=BG).pack(anchor=tk.W, pady=(0, 6))

        tk.Button(inner, text="Save Settings", font=(FONT, 12, "bold"),
                  bg="#1d9bf0", fg="#ffffff", activebackground="#0b7fd4",
                  activeforeground="#ffffff", relief=tk.FLAT, pady=8, cursor="hand2",
                  command=self._save_from_panel).pack(fill=tk.X, pady=(6, 18))

    def _refresh_settings_vars(self):
        s = self.settings
        sv = self._sv
        sv["prov"].set(s.get("llm_provider", "local"))
        sv["url"].set(s.get("llm_api_url", ""))
        sv["key"].set(s.get("llm_api_key", ""))
        sv["model"].set(s.get("llm_api_model", "gpt-4o-mini"))
        sv["hk"].set(s.get("hotkey", "alt_r"))
        sv["launch"].set(s.get("launch_hotkey", "ctrl+alt+l"))
        sv["pin"].set(bool(s.get("always_on_top", True)))
        sv["mini"].set(bool(s.get("mini_widget", True)))
        sv["mode"].set(s.get("mode", "both"))
        sv["safe"].set(bool(s.get("safe_shutdown", True)))
        opts, mmap = self._mic_options()
        sv["mic_map"] = mmap
        sv["mic_combo"].configure(values=opts)
        sv["mic"].set(opts[0])
        for i, d in mmap.items():
            if d == s.get("mic_device"):
                sv["mic"].set(opts[i])
                break

    def _save_from_panel(self):
        sv = self._sv
        self.settings["llm_provider"] = sv["prov"].get()
        self.settings["llm_api_url"] = sv["url"].get().strip()
        self.settings["llm_api_key"] = sv["key"].get().strip()
        self.settings["llm_api_model"] = sv["model"].get().strip() or "gpt-4o-mini"
        self.settings["hotkey"] = sv["hk"].get()
        self.settings["launch_hotkey"] = sv["launch"].get().strip() or "ctrl+alt+l"
        self.settings["mode"] = sv["mode"].get()
        self.settings["safe_shutdown"] = bool(sv["safe"].get())
        self.settings["always_on_top"] = bool(sv["pin"].get())
        self.settings["mini_widget"] = bool(sv["mini"].get())
        try:
            self.settings["mic_device"] = sv["mic_map"].get(sv["mic_combo"].current(), None)
        except Exception:
            pass
        save_settings(self.settings)
        self.root.attributes("-topmost", bool(self.settings["always_on_top"]))
        if self.settings["mini_widget"]:
            self._create_mini()
        else:
            self._destroy_mini()
        if self._on_mode:
            self._on_mode(self.settings["mode"])
        if self._on_settings_change:
            self._on_settings_change(self.settings)
        self._back_to_orb()

    def destroy(self):
        if threading.current_thread() is not threading.main_thread():
            return
        try:
            self._destroy_mini()
        except Exception:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass


if __name__ == "__main__":
    ui = StatusUI()
    ui.set_state("ready", "Say “Hey Laya”…")
    ui.set_timer(600.0)
    for s, d in (("listen", "Listening…"), ("exec", "Setting timer…"),
                 ("error", "Mic offline"), ("ready", "Say “Hey Laya”…")):
        ui.set_state(s, d)
        for _ in range(30):
            ui.tick()
            time.sleep(0.02)
    ui.set_timer(None)
    print("ui.demo OK")
    ui.root.destroy()
