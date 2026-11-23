"""Single background thread that performs all Instagram traffic, one job at a time."""
from __future__ import annotations

import itertools
import logging
import os
import queue
import time
import traceback
from dataclasses import dataclass, field
from typing import Optional

from PySide6.QtCore import QThread, Signal

from .fetcher import AccountGone, Fetcher
from .ig_client import AnonClient, IGError
from .store import Store
from .throttle import Cancelled, Throttle

log = logging.getLogger("igreader.worker")

PRIO = {"add": 0, "older": 1, "refresh": 2}


@dataclass(order=True)
class Job:
    prio: int
    seq: int
    kind: str = field(compare=False)
    username: Optional[str] = field(compare=False, default=None)
    batch: Optional[int] = field(compare=False, default=None)


class Worker(QThread):
    postsChanged = Signal(str)
    accountsChanged = Signal()
    progress = Signal(int, int, str)  # done, total, text  (total 0 = hide bar)
    status = Signal(str)
    failed = Signal(str, str)  # kind, message
    cooldownChanged = Signal()
    idle = Signal(bool)
    accountAdded = Signal(str)

    def __init__(self, store: Store, fake: bool = False):
        super().__init__()
        self.store = store
        self.fake = fake
        self.q: "queue.PriorityQueue[Job]" = queue.PriorityQueue()
        self.seq = itertools.count()
        self.throttle = Throttle(status=self.status.emit, scale=0.05 if fake else 1.0)
        self.pending: set[tuple[str, Optional[str]]] = set()
        self.batch_id = 0
        self.batch_total = 0
        self.batch_done = 0
        self.batch_blocked = False
        self._client = None
        self._last_refresh_user: Optional[str] = None

    # --- API for the GUI thread ----------------------------------------------
    def submit(self, kind: str, username: Optional[str] = None) -> bool:
        key = (kind, username)
        if key in self.pending:
            return False
        if self.store.cooldown_until() and kind != "add":
            return False
        self.pending.add(key)
        self.q.put(Job(PRIO[kind], next(self.seq), kind, username))
        return True

    def refresh(self, usernames: list[str]) -> int:
        usernames = [u for u in usernames if ("refresh", u) not in self.pending]
        if not usernames or self.store.cooldown_until():
            return 0
        self.batch_id += 1
        if self.batch_done >= self.batch_total:
            self.batch_total = self.batch_done = 0
            self.batch_blocked = False
        self.batch_total += len(usernames)
        for u in usernames:
            self.pending.add(("refresh", u))
            self.q.put(Job(PRIO["refresh"], next(self.seq), "refresh", u, self.batch_id))
        self.progress.emit(self.batch_done, self.batch_total, "Refreshing…")
        return len(usernames)

    def is_pending(self, kind: str, username: Optional[str]) -> bool:
        return (kind, username) in self.pending

    def shutdown(self) -> None:
        self.throttle.stop()
        self.q.put(Job(-2, next(self.seq), "quit"))
        self.wait(5000)

    # --- internals (worker thread) -------------------------------------------
    def posts_changed(self, username: str) -> None:
        self.postsChanged.emit(username)

    def accounts_changed(self) -> None:
        self.accountsChanged.emit()

    def client(self):
        if self._client is None:
            if self.fake:
                from .fake_client import FakeClient
                self._client = FakeClient(self.throttle, self.store.root)
            else:
                self._client = AnonClient(self.throttle)
        return self._client

    def _drop_queued(self) -> None:
        dropped = 0
        try:
            while True:
                j = self.q.get_nowait()
                if j.kind == "quit":
                    self.q.put(j)
                    break
                self.pending.discard((j.kind, j.username))
                dropped += 1
        except queue.Empty:
            pass
        self.batch_total = self.batch_done = 0
        self.progress.emit(0, 0, "")

    def run(self) -> None:
        busy = False
        while True:
            try:
                job = self.q.get(timeout=0.5)
            except queue.Empty:
                if busy:
                    busy = False
                    if self.batch_total and self.batch_done >= self.batch_total:
                        if not self.batch_blocked:
                            self.store.clear_cooldown_level()
                            self.cooldownChanged.emit()
                        self.batch_total = self.batch_done = 0
                        self.progress.emit(0, 0, "")
                    self.status.emit("Idle")
                    self.idle.emit(True)
                continue
            if job.kind == "quit":
                return
            if not busy:
                busy = True
                self.idle.emit(False)
            try:
                self._run_job(job)
            except Cancelled:
                return
            finally:
                self.pending.discard((job.kind, job.username))
                if job.kind == "refresh":
                    self.batch_done += 1
                    if self.batch_total:
                        self.progress.emit(self.batch_done, self.batch_total,
                                           f"Refreshed {self.batch_done}/{self.batch_total}")

    def _run_job(self, job: Job) -> None:
        if self.store.cooldown_until() and job.kind != "add":
            return
        if job.kind == "refresh" and self.store.account(job.username) is None:
            return
        try:
            client = self.client()
            fetcher = Fetcher(self.store, client, self, status=self.status.emit)
            if job.kind == "add":
                if self.store.cooldown_until():
                    raise IGError("paused", "Fetching is paused (cooldown) — try adding again later")
                acc = fetcher.add_account(job.username)
                self.accountAdded.emit(acc.username)
                self.status.emit(f"Added @{acc.username}")
            elif job.kind == "older":
                username = job.username or self.store.limiting_account()
                if username:
                    fetcher.extend_history(username)
            elif job.kind == "refresh":
                if self._last_refresh_user and self._last_refresh_user != job.username:
                    self.throttle.between_accounts()
                self._last_refresh_user = job.username
                self.progress.emit(self.batch_done, self.batch_total, f"Refreshing @{job.username}…")
                n = fetcher.refresh(job.username)
                self.status.emit(f"@{job.username}: {n} new post{'s' if n != 1 else ''}")
        except Cancelled:
            raise
        except AccountGone:
            pass
        except IGError as e:
            self._handle_error(job, e)
        except Exception as e:  # noqa: BLE001
            log.error("job %s failed:\n%s", job, traceback.format_exc())
            self._handle_error(job, IGError("other", f"{type(e).__name__}: {e}"))

    def _handle_error(self, job: Job, e: IGError) -> None:
        log.warning("job %s %s: %s (%s)", job.kind, job.username, e, e.kind)
        if e.kind == "block":
            if job.kind != "add" or not self.store.cooldown_until():
                until = self.store.start_cooldown(str(e))
                log.warning("cooldown until %s", time.strftime("%Y-%m-%d %H:%M", time.localtime(until)))
            self.batch_blocked = True
            self._drop_queued()
            self.cooldownChanged.emit()
        elif e.kind == "network":
            self._drop_queued()
        elif e.kind == "account" and job.username and job.kind != "add":
            acc = self.store.account(job.username)
            if acc:
                acc.last_error = str(e)
                self.store.save_account(acc)
                self.accountsChanged.emit()
        self.failed.emit(e.kind, str(e))
