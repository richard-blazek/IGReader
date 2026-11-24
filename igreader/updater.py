"""Keeps instaloader up to date (it tracks Instagram's changes to the logged-out web app).

Once a day, when online and idle, `pip install --upgrade` instaloader inside the app's venv (talks
to PyPI only, never to Instagram). A new version must pass `ig_client.self_check()` (offline) or
it is rolled back at once. If it passes, the app restarts itself the next time the window is
hidden and the worker idle, and runs the new version "on trial": the first fetch that crashes
because our code and instaloader don't fit together (`IGError.code_error`) rolls it back again;
the first fetch that works ends the trial. Rolled-back versions are never installed again.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import subprocess
import sys
import sysconfig
import threading
import time
from typing import Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal

log = logging.getLogger("igreader.update")

PACKAGE = "instaloader"
SPEC = "instaloader>=4.16,<5"  # keep in step with pyproject.toml
CHECK_EVERY = 24 * 3600
FIRST_CHECK_AFTER = 15 * 60
TICK_MS = 10 * 60 * 1000

Runner = Callable[[list[str]], tuple[int, str]]


def run_cmd(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)


def can_self_update() -> Optional[str]:
    """None if pip can update the app's own environment, else the reason why not."""
    if sys.prefix == sys.base_prefix:
        return "not running in a virtualenv (won't touch the system Python)"
    if importlib.util.find_spec("pip") is None:
        return "pip is not installed in the virtualenv"
    if not os.access(sysconfig.get_paths()["purelib"], os.W_OK):
        return "the virtualenv is not writable"
    return None


class Updater(QObject):
    _finished = Signal(object)  # result dict from the background thread

    def __init__(self, store, worker, can_restart: Callable[[], bool], restart: Callable[[], None],
                 now: Callable[[], float] = time.time, online: Optional[Callable[[], bool]] = None,
                 runner: Runner = run_cmd, parent=None):
        super().__init__(parent)
        self.store = store
        self.worker = worker
        self.can_restart = can_restart
        self.restart = restart
        self.now = now
        if online is None:
            from .background import _network_online
            online = _network_online
        self.online = online
        self.runner = runner
        self.started_at = now()
        self.busy = False
        self.restart_pending = False
        self._finished.connect(self._on_finished)
        worker.codeError.connect(self._on_code_error)
        worker.jobSucceeded.connect(self._on_job_ok)
        self.timer = QTimer(self, interval=TICK_MS)
        self.timer.timeout.connect(self.tick)

    # --- state ---------------------------------------------------------------------------
    @property
    def st(self) -> dict:
        return self.store.state.setdefault("updates", {})

    def _save(self) -> None:
        self.store.save_state()

    def spec(self) -> str:
        return SPEC + "".join(f",!={v}" for v in self.st.get("bad", []))

    # --- commands --------------------------------------------------------------------------
    def _py(self, *args: str) -> list[str]:
        return [sys.executable, *args]

    def installed_version(self) -> Optional[str]:
        code, out = self.runner(self._py(
            "-c", f"import importlib.metadata as m; print(m.version({PACKAGE!r}))"))
        return out.splitlines()[-1].strip() if code == 0 and out else None

    def _pip(self, requirement: str, upgrade: bool) -> tuple[int, str]:
        cmd = self._py("-m", "pip", "install", "-q", "--disable-pip-version-check")
        if upgrade:
            cmd += ["--upgrade", "--upgrade-strategy", "only-if-needed"]
        return self.runner(cmd + [requirement])

    def _self_check(self) -> tuple[int, str]:
        return self.runner(self._py("-c", "from igreader.ig_client import self_check; self_check()"))

    def _rollback(self, old: str, bad: str) -> dict:
        code, out = self._pip(f"{PACKAGE}=={old}", upgrade=False)
        if code != 0:
            log.error("rolling back to %s %s failed: %s", PACKAGE, old, out[-2000:])
        return {"rolled_back": code == 0, "old": old, "bad": bad}

    # --- lifecycle -----------------------------------------------------------------------
    def start(self) -> None:
        trial = self.st.get("trial")
        if trial:
            current = self.installed_version()
            if current != trial.get("new"):
                self.st.pop("trial", None)
                self._save()
            else:
                log.info("%s %s on trial (previous: %s)", PACKAGE, trial["new"], trial["old"])
        self.timer.start()

    def tick(self) -> None:
        if self.busy:
            return
        if self.restart_pending:
            if self.can_restart():
                log.info("restarting to load the updated %s", PACKAGE)
                self.restart()
            return
        if self.worker.pending:
            return
        now = self.now()
        if now - self.started_at < FIRST_CHECK_AFTER or now - self.st.get("checked_at", 0) < CHECK_EVERY:
            return
        if self.st.get("trial") or not self.online():
            return
        why = can_self_update()
        if why:
            log.info("not updating %s: %s", PACKAGE, why)
            self.st["checked_at"] = int(now)
            self._save()
            return
        self.st["checked_at"] = int(now)
        self._save()
        self.busy = True
        spec = self.spec()
        threading.Thread(target=lambda: self._finished.emit(self._update(spec)), daemon=True).start()

    def _update(self, spec: str) -> dict:
        """Runs in a background thread; only subprocesses, no app state."""
        old = self.installed_version()
        code, out = self._pip(spec, upgrade=True)
        if code != 0:
            return {"error": f"pip failed: {out[-2000:]}"}
        new = self.installed_version()
        if not old or not new or new == old:
            return {"unchanged": new or old}
        code, out = self._self_check()
        if code != 0:
            log.warning("%s %s failed the self-check:\n%s", PACKAGE, new, out[-2000:])
            return self._rollback(old, new)
        return {"updated": True, "old": old, "new": new}

    def _on_finished(self, r: dict) -> None:
        self.busy = False
        if "error" in r:
            log.warning("%s update check: %s", PACKAGE, r["error"])
        elif "unchanged" in r:
            log.info("%s %s is up to date", PACKAGE, r["unchanged"])
        elif "rolled_back" in r:
            self._mark_bad(r["bad"])
            log.warning("%s %s rejected; %s", PACKAGE, r["bad"],
                        f"back on {r['old']}" if r["rolled_back"] else "ROLLBACK FAILED")
            if r.get("restart"):  # the broken version was loaded in this process
                self.restart_pending = True
                self.tick()
        elif r.get("updated"):
            log.info("%s %s → %s installed and passed the self-check; restart pending",
                     PACKAGE, r["old"], r["new"])
            self.st["trial"] = {"old": r["old"], "new": r["new"]}
            self._save()
            self.restart_pending = True
            self.tick()

    def _mark_bad(self, version: str) -> None:
        bad = self.st.setdefault("bad", [])
        if version not in bad:
            bad.append(version)
        self._save()

    # --- trial outcome -------------------------------------------------------------------
    def _on_job_ok(self) -> None:
        trial = self.st.pop("trial", None)
        if trial:
            log.info("%s %s works; trial over", PACKAGE, trial["new"])
            self._save()

    def _on_code_error(self, msg: str) -> None:
        trial = self.st.get("trial")
        if not trial or self.busy:
            return
        log.warning("%s %s broke fetching (%s); rolling back to %s", PACKAGE, trial["new"], msg, trial["old"])
        self.st.pop("trial", None)
        self._save()
        self.busy = True
        old, new = trial["old"], trial["new"]

        def go():
            r = self._rollback(old, new)
            r["restart"] = r["rolled_back"]
            self._finished.emit(r)
        threading.Thread(target=go, daemon=True).start()
