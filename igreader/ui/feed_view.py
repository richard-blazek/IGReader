"""Scrolling feed. Shows cached posts in batches; asks for older pages at the bottom."""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QLabel, QScrollArea, QVBoxLayout, QWidget

from ..store import Store
from .post_widget import PostWidget

BATCH = 15


class FeedView(QScrollArea):
    needOlder = Signal(object)  # username or None (merged feed)
    authorClicked = Signal(str)

    def __init__(self, store: Store, footer_state: Callable[[Optional[str]], tuple[str, bool]], parent=None):
        """footer_state(username) -> (text, may_request_older)"""
        super().__init__(parent)
        self.store = store
        self.footer_state = footer_state
        self.username: Optional[str] = None
        self.widgets: dict[str, PostWidget] = {}
        self.shown: list[str] = []
        self.order: list[str] = []
        self.complete = False

        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        self.lay = QVBoxLayout(inner)
        self.lay.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.lay.setSpacing(18)
        self.lay.setContentsMargins(12, 12, 12, 12)
        self.footer = QLabel()
        self.footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.footer.setStyleSheet("color:#888; padding:24px;")
        self.footer.setOpenExternalLinks(False)
        self.footer.linkActivated.connect(lambda _: self.needOlder.emit(self.username))
        self.lay.addWidget(self.footer)
        self.setWidget(inner)

        self._reload_timer = QTimer(self, singleShot=True, interval=250)
        self._reload_timer.timeout.connect(self.reload)
        self.verticalScrollBar().valueChanged.connect(self._scrolled)

    # --- public ---------------------------------------------------------------
    def set_source(self, username: Optional[str]) -> None:
        self.username = username
        for w in self.widgets.values():
            w.unload()
            self.lay.removeWidget(w)
            w.hide()
            w.deleteLater()
        self.widgets.clear()
        self.shown = []
        self.verticalScrollBar().setValue(0)
        self.reload()

    def schedule_reload(self) -> None:
        if not self._reload_timer.isActive():
            self._reload_timer.start()

    def reload(self) -> None:
        posts, self.complete = self.store.feed(self.username)
        by_sc = {p.shortcode: p for p in posts}
        self.order = [p.shortcode for p in posts]
        # Keep showing at least down to the previously last shown post.
        if self.shown and self.shown[-1] in by_sc:
            n = self.order.index(self.shown[-1]) + 1
        else:
            n = min(len(self.order), max(len(self.shown), BATCH))
        self._apply(self.order[:max(n, min(BATCH, len(self.order)))], by_sc)

    def check_more(self) -> None:
        self._scrolled(self.verticalScrollBar().value())

    # --- internals ------------------------------------------------------------
    def _apply(self, target: list[str], by_sc) -> None:
        bar = self.verticalScrollBar()
        anchor, offset = None, 0
        if bar.value() > 0:
            for sc in self.shown:
                w = self.widgets.get(sc)
                if w and w.y() + w.height() > bar.value():
                    anchor, offset = sc, w.y() - bar.value()
                    break
        tset = set(target)
        for sc in list(self.widgets):
            post = by_sc.get(sc)
            w = self.widgets[sc]
            if sc not in tset or post is None:
                w.unload()
                self.lay.removeWidget(w)
                w.hide()
                w.deleteLater()
                del self.widgets[sc]
        for i, sc in enumerate(target):
            w = self.widgets.get(sc)
            if w is None:
                post = by_sc[sc]
                w = PostWidget(post, self.store.account_dir(post.username),
                               lambda u: self.store.account(u) is not None)
                w.authorClicked.connect(self.authorClicked)
                self.widgets[sc] = w
                self.lay.insertWidget(i, w, 0, Qt.AlignmentFlag.AlignHCenter)
            elif self.lay.indexOf(w) != i:
                self.lay.removeWidget(w)
                self.lay.insertWidget(i, w, 0, Qt.AlignmentFlag.AlignHCenter)
        self.shown = list(target)
        self._update_footer()
        if anchor and anchor in self.widgets:
            w = self.widgets[anchor]
            QTimer.singleShot(0, lambda: bar.setValue(w.y() - offset))
        QTimer.singleShot(0, self.check_more)

    def _update_footer(self) -> None:
        if len(self.shown) < len(self.order):
            self.footer.setText("…")
            return
        text, _ = self.footer_state(self.username) if not self.complete else (self._end_text(), False)
        self.footer.setText(text)

    def _end_text(self) -> str:
        if not self.order:
            return "No posts." if self.username else "No posts yet — add an account with “+ Add”."
        return "— Beginning of history —"

    def _scrolled(self, value: int) -> None:
        vh = self.viewport().height()
        # lazy media loading / unloading
        for sc in self.shown:
            w = self.widgets.get(sc)
            if w is None:
                continue
            top, bottom = w.y(), w.y() + w.height()
            if bottom > value - 2 * vh and top < value + 3 * vh:
                w.load()
            elif bottom < value - 6 * vh or top > value + 7 * vh:
                w.unload()
        bar = self.verticalScrollBar()
        if value < bar.maximum() - 1.5 * vh:
            return
        if len(self.shown) < len(self.order):
            by_sc = {p.shortcode: p for p in self.store.feed(self.username)[0]}
            self._apply(self.order[:len(self.shown) + BATCH], by_sc)
        elif not self.complete:
            text, may = self.footer_state(self.username)
            self.footer.setText(text)
            if may:
                self.needOlder.emit(self.username)
