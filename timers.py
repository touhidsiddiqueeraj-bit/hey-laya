"""Background timers — thread-safe countdown that fires a callback."""
from __future__ import annotations

import threading
from typing import Callable


class Timer:
    def __init__(self, minutes: float, on_done: Callable[[], None]):
        self.minutes = minutes
        self._on_done = on_done
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()

    def start(self) -> "Timer":
        def run():
            if self._cancel.wait(self.minutes * 60):
                return
            self._on_done()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        return self

    def cancel(self):
        self._cancel.set()


class TimerBank:
    def __init__(self):
        self._timers: list[Timer] = []

    def set(self, minutes: float, on_done: Callable[[], None]) -> Timer:
        t = Timer(minutes, on_done).start()
        self._timers.append(t)
        return t

    def cancel_all(self):
        for t in self._timers:
            t.cancel()
        self._timers.clear()


def demo():
    # Self-check: 0.01 min ≈ 0.6s
    fired = threading.Event()
    bank = TimerBank()
    bank.set(0.01, fired.set)
    assert fired.wait(2), "timer did not fire"
    bank.cancel_all()
    print("timers.demo OK")


if __name__ == "__main__":
    demo()
