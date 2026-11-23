"""Request pacing shared by everything that talks to Instagram."""
from __future__ import annotations

import random
import threading
import time
from typing import Callable, Optional


class Cancelled(Exception):
    """Raised inside the worker when the app is shutting down."""


class Throttle:
    """Enforces randomized minimum gaps between requests and interruptible sleeps.

    Only one worker thread issues requests, so this is about pacing, not concurrency."""

    API_GAP = (5.0, 12.0)  # between Instagram requests (profile page, timeline page, post metadata)
    MEDIA_GAP = (0.5, 1.5)  # between CDN media downloads
    ACCOUNT_GAP = (20.0, 60.0)  # between accounts during a multi-account refresh

    def __init__(self, status: Optional[Callable[[str], None]] = None, scale: float = 1.0):
        self._stop = threading.Event()
        self._last_api = 0.0
        self._last_media = 0.0
        self.status = status or (lambda msg: None)
        self.scale = scale  # tests / fake mode use a small scale

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def sleep(self, secs: float, why: str = "", scaled: bool = True) -> None:
        """Interruptible sleep. `scaled` applies the test/fake speed-up factor."""
        if scaled:
            secs *= self.scale
        if secs <= 0:
            return
        if secs >= 3 and why:
            self.status(f"{why} — waiting {secs:.0f}s")
        if self._stop.wait(secs):
            raise Cancelled()

    def _gap(self, last: float, rng: tuple[float, float], why: str) -> None:
        target = random.uniform(*rng) * self.scale
        elapsed = time.monotonic() - last
        if elapsed < target:
            self.sleep(target - elapsed, why, scaled=False)

    def before_api(self) -> None:
        self.check()
        self._gap(self._last_api, self.API_GAP, "Pacing requests")
        self._last_api = time.monotonic()

    def before_media(self) -> None:
        self.check()
        self._gap(self._last_media, self.MEDIA_GAP, "")
        self._last_media = time.monotonic()

    def between_accounts(self) -> None:
        self.sleep(random.uniform(*self.ACCOUNT_GAP), "Pausing between accounts")

    def check(self) -> None:
        if self._stop.is_set():
            raise Cancelled()
