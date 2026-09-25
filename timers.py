"""Background timers — thread-safe countdown that fires a callback."""
from __future__ import annotations

import threading
import time
from typing import Callable


class Timer:
    def __init__(self, minutes: float, on_done: Callable[[], None]):
        self.minutes = minutes
        self._on_done = on_done
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self.ends_at = time.monotonic() + minutes * 60

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

    def remaining(self) -> float:
        """Seconds left (0 when fired/cancelled)."""
        if self._cancel.is_set():
            return 0.0
        return max(0.0, self.ends_at - time.monotonic())


class TimerBank:
    def __init__(self):
        self._timers: list[Timer] = []
        self._lock = threading.Lock()

    def set(self, minutes: float, on_done: Callable[[], None]) -> Timer:
        t = Timer(minutes, on_done).start()
        with self._lock:
            self._timers.append(t)
        return t

    def cancel_all(self):
        with self._lock:
            timers, self._timers = self._timers, []
        for t in timers:
            t.cancel()

    def soonest_remaining(self) -> float | None:
        """Seconds until the next active timer fires (None when no timer)."""
        with self._lock:
            self._timers = [t for t in self._timers if t.remaining() > 0]
            if not self._timers:
                return None
            return min(t.remaining() for t in self._timers)


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
