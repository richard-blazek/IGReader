from __future__ import annotations

import logging
import time
from typing import Optional

from PySide6.QtCore import QDateTime, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QIcon, QKeySequence
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem,
                               QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton, QSplitter,
                               QVBoxLayout, QWidget)

from ..store import Store
from ..worker import Worker
from .dialogs import parse_handle
from .feed_view import FeedView
from .post_widget import avatar, forget_avatar

ALL = "\x00all"
STARTUP_REFRESH_HOURS = 6  # on start / reopen, check accounts not checked for this long


class MainWindow(QMainWindow):
    def __init__(self, store: Store, worker: Worker, fake: bool):
        super().__init__()
        self.store = store
        self.worker = worker
        self.fake = fake
        self.adding: set[str] = set()
        self.older_failed_at: dict[Optional[str], float] = {}
        self.last_error: Optional[str] = None
        self.hidden_since_start = False  # started with --background, never shown yet
        self._quitting = False
        self.setWindowTitle("IGReader" + (" — offline demo data" if fake else ""))
        self.resize(1000, 900)

        # sidebar
        self.list = QListWidget()
        self.list.setIconSize(QSize(28, 28))
        self.list.currentItemChanged.connect(self._select)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)
        add = QPushButton("+ Add")
        add.clicked.connect(self.add_account)
        rem = QPushButton("− Remove")
        rem.clicked.connect(self.remove_account)
        side_btns = QHBoxLayout()
        side_btns.addWidget(add)
        side_btns.addWidget(rem)
        side = QWidget()
        sl = QVBoxLayout(side)
        sl.setContentsMargins(6, 6, 0, 6)
        sl.addWidget(self.list, 1)
        sl.addLayout(side_btns)

        # toolbar
        top = QHBoxLayout()
        top.addStretch(1)
        quit_btn = QPushButton("Force quit")
        quit_btn.setToolTip("Quit completely (stop background checks). Closing the window keeps "
                            "checking for new posts in the background.")
        quit_btn.clicked.connect(self.quit_app)
        top.addWidget(quit_btn)

        # banner
        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet("background:#fff3cd; color:#664d03; padding:8px; border-radius:4px;")
        self.banner.setTextFormat(Qt.TextFormat.RichText)
        self.banner.linkActivated.connect(self._banner_link)
        self.banner.hide()

        self.feed = FeedView(store, self._footer_state)
        self.feed.needOlder.connect(self._need_older)
        self.feed.authorClicked.connect(self.select_account)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(6, 6, 6, 0)
        rl.addLayout(top)
        rl.addWidget(self.banner)
        rl.addWidget(self.feed, 1)

        split = QSplitter()
        split.addWidget(side)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        split.setSizes([240, 760])
        self.setCentralWidget(split)

        self.status = QLabel("Idle")
        self.bar = QProgressBar()
        self.bar.setMaximumWidth(260)
        self.bar.setFormat("%v / %m accounts")
        self.bar.hide()
        self.statusBar().addWidget(self.status, 1)
        self.statusBar().addPermanentWidget(self.bar)

        rm = QAction(self)
        rm.setShortcut(QKeySequence.StandardKey.Delete)
        rm.triggered.connect(self.remove_account)
        self.list.addAction(rm)
        q = QAction(self)
        q.setShortcut(QKeySequence.StandardKey.Quit)
        q.triggered.connect(self.quit_app)
        self.addAction(q)

        w = worker
        w.postsChanged.connect(self._posts_changed)
        w.accountsChanged.connect(self.populate)
        w.accountAdded.connect(self._added)
        w.progress.connect(self._progress)
        w.status.connect(self.status.setText)
        w.failed.connect(self._failed)
        w.cooldownChanged.connect(self.update_banner)
        w.idle.connect(lambda idle: idle and self.isVisible() and self.feed.check_more())

        self.populate()
        self.list.setCurrentRow(0)
        self._select()
        self.update_banner()
        self._banner_timer = QTimer(self, interval=30_000)
        self._banner_timer.timeout.connect(self.update_banner)
        self._banner_timer.start()

    # --- sidebar ----------------------------------------------------------------
    def populate(self):
        cur = self.current_username()
        cur_key = ALL if cur is None else cur
        self.list.blockSignals(True)
        self.list.clear()
        it = QListWidgetItem("All accounts")
        it.setData(Qt.ItemDataRole.UserRole, ALL)
        f = it.font()
        f.setBold(True)
        it.setFont(f)
        self.list.addItem(it)
        select = 0
        for acc in sorted(self.store.accounts(), key=lambda a: a.username.casefold()):
            n = len(self.store.account_posts(acc.username))
            it = QListWidgetItem(f"@{acc.username}")
            forget_avatar(self.store.profile_pic(acc.username))
            it.setIcon(QIcon(avatar(self.store.profile_pic(acc.username), 28)))
            it.setData(Qt.ItemDataRole.UserRole, acc.username)
            last = (QDateTime.fromSecsSinceEpoch(acc.last_refresh_at).toString("d MMMM yyyy HH:mm")
                    if acc.last_refresh_at else "never")
            tip = [acc.full_name, f"{n} posts cached" + (" (full history)" if acc.history_complete else ""),
                   f"Last checked: {last}"]
            if acc.last_error:
                tip.append(f"⚠ {acc.last_error}")
                it.setText(f"@{acc.username}  ⚠")
            it.setToolTip("\n".join(t for t in tip if t))
            self.list.addItem(it)
            if acc.username == cur_key:
                select = self.list.count() - 1
        for u in sorted(self.adding):
            it = QListWidgetItem(f"@{u}  (adding…)")
            it.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(it)
        self.list.setCurrentRow(select)
        self.list.blockSignals(False)
        self.feed.update_bio()
        if (ALL if self.feed.username is None else self.feed.username) != (self.current_username() or ALL):
            self._select()

    def current_username(self) -> Optional[str]:
        it = self.list.currentItem()
        if it is None:
            return None
        key = it.data(Qt.ItemDataRole.UserRole)
        return None if key in (ALL, None) else key

    def select_account(self, username: str):
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.ItemDataRole.UserRole) == username:
                self.list.setCurrentRow(i)
                return

    def _select(self, *_):
        u = self.current_username()
        if self.feed.username != u or not self.feed.shown:
            self.feed.set_source(u)

    def _context_menu(self, pos):
        it = self.list.itemAt(pos)
        if not it or it.data(Qt.ItemDataRole.UserRole) in (ALL, None):
            return
        m = QMenu(self)
        m.addAction("Force refresh", self.refresh_selected)
        m.addAction("Remove…", self.remove_account)
        m.exec(self.list.mapToGlobal(pos))

    # --- actions ------------------------------------------------------------------
    def add_account(self):
        text, ok = QInputDialog.getText(self, "Add account", "Instagram handle or profile URL:")
        if not ok or not text.strip():
            return
        u = parse_handle(text)
        if not u:
            QMessageBox.warning(self, "Add account", f"“{text}” is not a valid Instagram handle.")
            return
        if self.store.account(u):
            self.select_account(u)
            return
        if not self._can_fetch(explain=True):
            return
        if self.worker.submit("add", u):
            self.adding.add(u)
            self.populate()

    def _added(self, username: str):
        self.adding.discard(username)
        self.populate()
        self.select_account(username)

    def remove_account(self):
        u = self.current_username()
        if not u:
            return
        n = len(self.store.account_posts(u))
        r = QMessageBox.question(
            self, "Remove account",
            f"Remove @{u}?\n\nIts {n} cached posts and all downloaded photos/videos will be deleted from disk.")
        if r != QMessageBox.StandardButton.Yes:
            return
        self.store.remove_account(u)
        self.list.setCurrentRow(0)
        self.populate()
        self.feed.set_source(None)

    def refresh_selected(self):
        u = self.current_username()
        if u and self._can_fetch(explain=True):
            self.worker.refresh([u])

    def startup_refresh(self):
        """On start / reopen: check accounts not checked within STARTUP_REFRESH_HOURS."""
        if not self.store.accounts() or not self._can_fetch(explain=False):
            return
        now = time.time()
        due = [a.username for a in self.store.accounts()
               if not a.last_refresh_at or now - a.last_refresh_at >= STARTUP_REFRESH_HOURS * 3600]
        if due:
            self.worker.refresh(due)

    # --- worker events ------------------------------------------------------------------
    def _posts_changed(self, username: str):
        if self.isVisible() and self.feed.username in (None, username):
            self.feed.schedule_reload()

    def _progress(self, done: int, total: int, text: str):
        if total <= 0:
            self.bar.hide()
            return
        self.bar.setRange(0, total)
        self.bar.setValue(done)
        self.bar.show()
        if text:
            self.status.setText(text)

    def _failed(self, kind: str, msg: str):
        self.last_error = msg
        if kind in ("account", "paused") and self.adding:
            for u in list(self.adding):
                if u in msg or kind == "paused":
                    self.adding.discard(u)
            self.populate()
        if kind == "account" and self.isVisible() and not self.worker.is_pending("refresh", None):
            QMessageBox.warning(self, "Instagram", msg)
        if kind in ("other", "network"):
            self.older_failed_at[self.feed.username] = time.time()
            self.status.setText(f"⚠ {msg}")
        self.update_banner()
        if self.isVisible():
            self.feed.reload()

    def _need_older(self, username: Optional[str]):
        if not self.isVisible() or not self._can_fetch(explain=False):
            return
        if time.time() - self.older_failed_at.get(username, 0) < 120:
            return
        self.worker.submit("older", username)
        self.feed.footer.setText(self._footer_state(username)[0])

    # --- state -------------------------------------------------------------------------
    def _can_fetch(self, explain: bool) -> bool:
        until = self.store.cooldown_until()
        if until:
            if explain:
                QMessageBox.information(self, "Paused", "Fetching is paused after Instagram pushed back, until "
                                        f"{time.strftime('%H:%M (%d %b)', time.localtime(until))}.")
            return False
        return True

    def _footer_state(self, username: Optional[str]) -> tuple[str, bool]:
        if not self.store.accounts():
            return "No accounts yet — add one with “+ Add”.", False
        until = self.store.cooldown_until()
        if until:
            return f"Fetching older posts is paused until {time.strftime('%H:%M', time.localtime(until))}.", False
        if time.time() - self.older_failed_at.get(username, 0) < 120:
            return f"Couldn't load older posts: {self.last_error or ''} — <a href='retry'>retry</a>", False
        return "Loading older posts…", True

    def update_banner(self):
        until = self.store.cooldown_until()
        st = self.store.state
        msgs = []
        if until:
            msgs.append(f"<b>Fetching paused until {time.strftime('%H:%M, %d %b', time.localtime(until))}</b> — "
                        f"{(st.get('cooldown_reason') or 'Instagram pushed back').rstrip('.')}. Cached posts remain viewable. "
                        "<a href='resume'>Resume now…</a>")
        self.banner.setText("<br>".join(msgs))
        self.banner.setVisible(bool(msgs))

    def _banner_link(self, link: str):
        if link == "resume":
            r = QMessageBox.warning(
                self, "Resume fetching",
                "Instagram recently rate-limited anonymous requests from this network. Resuming early raises the "
                "risk of a longer block of this network's IP address.\n\nResume anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if r == QMessageBox.StandardButton.Yes:
                self.store.state["cooldown_until"] = None
                self.store.save_state()
                self.update_banner()
                self.feed.reload()

    # --- window lifecycle -----------------------------------------------------------
    def bring_to_front(self):
        was_hidden = not self.isVisible()
        first = self.hidden_since_start
        self.hidden_since_start = False
        self.showNormal()
        self.raise_()
        self.activateWindow()
        if was_hidden:
            self.populate()
            self.feed.reload()
            self.update_banner()
            QTimer.singleShot(1500 if first else 500, self.startup_refresh)

    def quit_app(self):
        self._quitting = True
        self.close()

    def closeEvent(self, e):
        if not self._quitting:
            e.ignore()
            self.hide()
            self.feed.set_source(self.feed.username)  # drop post widgets / images while hidden
            logging.getLogger("igreader").info("window closed; checking in the background")
            return
        self._quitting = True
        self.status.setText("Stopping…")
        super().closeEvent(e)
        QApplication.quit()  # aboutToQuit stops the worker
