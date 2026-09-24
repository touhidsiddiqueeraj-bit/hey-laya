"""Tkinter status window — white + light blue, always-on-top toggle, mode switch."""
from __future__ import annotations

import threading
import tkinter as tk

# Palette: white base, light blue = ready, deeper blue = listening, purple = executing
BG = "#ffffff"
READY = "#7ec8e3"     # light blue
LISTEN = "#2b7fd4"    # darker blue
EXEC = "#7c5cbf"      # purple
TEXT = "#1a1a2e"
MUTED = "#6b7280"
BORDER = "#e5e7eb"


class StatusUI:
    def __init__(self, on_mode_change=None, on_ptt_start=None, on_ptt_end=None):
        self.root = tk.Tk()
        self.root.title("Hey Laya")
        self.root.configure(bg=BG)
        self.root.geometry("320x215")
        self.root.resizable(False, False)
        self._on_mode = on_mode_change
        self._on_ptt_start = on_ptt_start
        self._on_ptt_end = on_ptt_end
        self._always_on_top = True
        self._pending = None  # (state, detail) queued by set_state, applied in tick()

        self.status = tk.Label(
            self.root, text="Ready", font=("Helvetica", 16, "bold"),
            bg=BG, fg=TEXT,
        )
        self.status.pack(pady=(14, 4))

        self.detail = tk.Label(
            self.root, text="Hold the button (or Space) · say “Hey Laya”",
            font=("Helvetica", 10), bg=BG, fg=MUTED, wraplength=280,
        )
        self.detail.pack(pady=(0, 8))

        # Color bar (state indicator)
        self.bar = tk.Canvas(self.root, height=6, bg=READY, highlightthickness=0)
        self.bar.pack(fill=tk.X, padx=16, pady=(0, 8))
        self.bar.create_rectangle(0, 0, 320, 6, fill=READY, outline="")

        # Hold-to-talk — works on Wayland where pynput global hotkeys don't
        self.talk = tk.Button(
            self.root, text="Hold to talk", font=("Helvetica", 12, "bold"),
            bg=LISTEN, fg="#ffffff", activebackground="#1f6bb0",
            activeforeground="#ffffff", relief=tk.FLAT, height=2, cursor="hand2",
        )
        self.talk.pack(fill=tk.X, padx=16, pady=(0, 8))
        self.talk.bind("<ButtonPress-1>", self._talk_down)
        self.talk.bind("<ButtonRelease-1>", self._talk_up)
        self.root.bind("<KeyPress-space>", self._key_down)
        self.root.bind("<KeyRelease-space>", self._key_up)

        controls = tk.Frame(self.root, bg=BG)
        controls.pack(pady=(0, 10))

        self.mode_var = tk.StringVar(value="both")
        for label, val in (("PTT", "ptt"), ("Wake", "wake"), ("Both", "both")):
            tk.Radiobutton(
                controls, text=label, variable=self.mode_var, value=val,
                bg=BG, fg=TEXT, selectcolor=BORDER, activebackground=BG,
                command=self._mode_changed,
            ).pack(side=tk.LEFT, padx=6)

        self.pin_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            controls, text="Always on top", variable=self.pin_var,
            bg=BG, fg=TEXT, selectcolor=BORDER, activebackground=BG,
            command=self._pin,
        ).pack(side=tk.LEFT, padx=6)

        self.root.attributes("-topmost", True)

    def _pin(self):
        self.root.attributes("-topmost", bool(self.pin_var.get()))

    def _talk_down(self, _event=None):
        if self._on_ptt_start:
            self._on_ptt_start()

    def _talk_up(self, _event=None):
        if self._on_ptt_end:
            self._on_ptt_end()

    def _key_down(self, event):
        if event.state & 0x1:  # ignore Shift+Space
            return
        self._talk_down()

    def _key_up(self, event):
        if event.state & 0x1:
            return
        self._talk_up()

    def _mode_changed(self):
        if self._on_mode:
            self._on_mode(self.mode_var.get())

    def set_state(self, state: str, detail: str = ""):
        """state: ready | listen | exec | error. Thread-safe: applies on next tick()."""
        self._pending = (state, detail)

    def _apply_state(self, state: str, detail: str = ""):
        colors = {"ready": READY, "listen": LISTEN, "exec": EXEC, "error": "#dc2626"}
        labels = {"ready": "Ready", "listen": "Listening…", "exec": "Working…", "error": "Error"}
        c = colors.get(state, READY)
        self.status.config(text=labels.get(state, state), fg=TEXT if state != "error" else colors["error"])
        self.bar.delete("all")
        self.bar.create_rectangle(0, 0, 320, 6, fill=c, outline="")
        if detail:
            self.detail.config(text=detail)

    def mode(self) -> str:
        return self.mode_var.get()

    def tick(self):
        """Process pending UI events; main thread only (Tk segfaults off-thread)."""
        if threading.current_thread() is not threading.main_thread():
            return  # pending set_state applies on the next main-thread tick
        if self._pending is not None:
            state, detail = self._pending
            self._pending = None
            self._apply_state(state, detail)
        try:
            self.root.update()
        except tk.TclError:
            pass

    def destroy(self):
        if threading.current_thread() is not threading.main_thread():
            return
        try:
            self.root.destroy()
        except tk.TclError:
            pass


if __name__ == "__main__":
    ui = StatusUI()
    ui.set_state("ready")
    # Brief state cycle demo then idle
    import time

    for s in ("listen", "exec", "ready"):
        ui.set_state(s)
        for _ in range(30):
            ui.tick()
            time.sleep(0.01)
    print("ui.demo OK — close window to exit")
    ui.root.mainloop()
