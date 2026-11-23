"""Low-key background checking for new posts while the window is closed, plus autostart.

Policy (kept deliberately modest): only during the day, only when online on a non-metered
connection, never during a cooldown, never while other jobs run, and at most every few hours
(randomized). A check only refreshes the newest posts of accounts not checked recently, through
the same throttled worker as a manual refresh; older history is never fetched in the background.
"""
from __future__ import annotations

import logging
import os
import random
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, QTimer

log = logging.getLogger("igreader.background")

DAY_START = 8  # local hour; plus a random 0–45 min offset, re-picked every night
DAY_END = 22
MORNING_JITTER_MIN = 45
MIN_AGE = 3 * 3600  # only accounts not checked within this long
GAP = (2.5 * 3600, 4 * 3600)  # between background checks
FIRST_GAP = (30 * 60, 60 * 60)  # after a normal start (the startup refresh just ran)
FIRST_GAP_HIDDEN = (2 * 60, 10 * 60)  # after an autostart at login (no startup refresh)
TICK_MS = 10 * 60 * 1000


def _network_online() -> bool:
    """True unless Qt positively knows we're offline or on a metered connection."""
    try:
        from PySide6.QtNetwork import QNetworkInformation
        if QNetworkInformation.instance() is None and not QNetworkInformation.loadDefaultBackend():
            return True  # no backend: just try; a network error costs nothing
        ni = QNetworkInformation.instance()
        if ni is None:
            return True
        if ni.supports(QNetworkInformation.Feature.Metered) and ni.isMetered():
            return False
        if ni.supports(QNetworkInformation.Feature.Reachability):
            return ni.reachability() == QNetworkInformation.Reachability.Online
    except Exception:  # noqa: BLE001
        log.debug("network information unavailable", exc_info=True)
    return True


class BackgroundScheduler(QObject):
    def __init__(self, store, worker, started_hidden: bool = False,
                 now: Callable[[], float] = time.time,
                 online: Callable[[], bool] = _network_online, parent=None):
        super().__init__(parent)
        self.store = store
        self.worker = worker
        self.now = now
        self.online = online
        self._morning_offset = random.uniform(0, MORNING_JITTER_MIN)
        self.next_at = self.now() + random.uniform(*(FIRST_GAP_HIDDEN if started_hidden else FIRST_GAP))
        self.timer = QTimer(self, interval=TICK_MS)
        self.timer.timeout.connect(self.tick)

    def start(self) -> None:
        self.timer.start()

    def is_daytime(self) -> bool:
        t = time.localtime(self.now())
        minutes = t.tm_hour * 60 + t.tm_min
        if minutes < DAY_START * 60 + self._morning_offset or minutes >= DAY_END * 60:
            if minutes >= DAY_END * 60 or minutes < DAY_START * 60:
                self._morning_offset = random.uniform(0, MORNING_JITTER_MIN)
            return False
        return True

    def due_accounts(self) -> list[str]:
        now = self.now()
        return [a.username for a in self.store.accounts()
                if not a.last_refresh_at or now - a.last_refresh_at >= MIN_AGE]

    def tick(self) -> Optional[int]:
        """Start a background check if everything allows it. Returns the number of accounts
        queued, or None if no check was made."""
        if self.now() < self.next_at or not self.is_daytime():
            return None
        if self.store.cooldown_until() or self.worker.pending:
            return None
        if not self.online():
            return None
        self.next_at = self.now() + random.uniform(*GAP)
        due = self.due_accounts()
        n = self.worker.refresh(due) if due else 0
        log.info("background check: %d account(s) queued; next check around %s", n,
                 time.strftime("%H:%M", time.localtime(self.next_at)))
        return n


# --- autostart at login -------------------------------------------------------------------

def autostart_file() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "autostart" / "igreader.desktop"


def _exec_line() -> str:
    arg0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if not arg0 or arg0.endswith(".py") or not os.access(arg0, os.X_OK):
        parts = [sys.executable, "-m", "igreader"]
    else:
        parts = [arg0]
    quoted = [f'"{p}"' if " " in p else p for p in parts]
    return " ".join(quoted + ["--background"])


def install_autostart() -> None:
    """Make sure the app starts hidden at login (rewritten only if it changed)."""
    path = autostart_file()
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=IGReader (background)\n"
        "Comment=Quietly checks for new posts during the day\n"
        f"Exec={_exec_line()}\n"
        "Icon=camera-photo\n"
        "Terminal=false\n"
        "NoDisplay=true\n"
        "X-GNOME-Autostart-enabled=true\n"
    )
    try:
        if path.exists() and path.read_text(encoding="utf-8") == content:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        log.info("autostart entry written: %s", path)
    except OSError as e:
        log.warning("could not write autostart entry: %s", e)
