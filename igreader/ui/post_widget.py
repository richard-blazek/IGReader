"""One post: author, date & time, media carousel, caption."""
from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QDateTime, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QBrush, QDesktopServices, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from ..store import Post
from .media_viewer import WIDTH, MediaViewer

_avatar_cache: dict[tuple[str, int], QPixmap] = {}

URL_RE = re.compile(r"(https?://[^\s<]+)|(?<![\w@])@([A-Za-z0-9._]{1,30})")
ACCOUNT_LINK = "igreader-account:"  # href prefix of mentions of tracked accounts, opened in the app


def avatar(path: Path, size: int) -> QPixmap:
    key = (str(path), size)
    if key in _avatar_cache:
        return _avatar_cache[key]
    src = QPixmap(str(path))
    out = QPixmap(size, size)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    clip = QPainterPath()
    clip.addEllipse(0, 0, size, size)
    if src.isNull():
        p.fillPath(clip, QBrush(Qt.GlobalColor.lightGray))
    else:
        p.setClipPath(clip)
        p.drawPixmap(0, 0, src.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                      Qt.TransformationMode.SmoothTransformation))
    p.end()
    _avatar_cache[key] = out
    return out


def forget_avatar(path: Path) -> None:
    for k in [k for k in _avatar_cache if k[0] == str(path)]:
        del _avatar_cache[k]


_LONG_RE = re.compile(r"\S{25,}")


def _breakable(text: str) -> str:
    """Escape text, adding zero-width break points into very long unbroken runs
    (URLs, emoji chains, …) so word wrap can never push them past the box edge."""
    def split(m: re.Match) -> str:
        s = m.group(0)
        return "\u200b".join(s[i:i + 20] for i in range(0, len(s), 20))
    return html.escape(_LONG_RE.sub(split, text))


def caption_html(text: str, tracked: Callable[[str], bool]) -> str:
    """Rich text with clickable web links and mentions of tracked accounts (tracked(username),
    lowercase); other mentions and hashtags stay plain text."""
    out, pos = [], 0
    for m in URL_RE.finditer(text):
        url, user = m.groups()
        if user and not tracked(user.lower()):
            continue
        out.append(_breakable(text[pos:m.start()]))
        if url:
            out.append(f'<a href="{html.escape(url)}">{_breakable(url)}</a>')
        else:
            out.append(f'<a href="{ACCOUNT_LINK}{html.escape(user.lower())}">@{html.escape(user)}</a>')
        pos = m.end()
    out.append(_breakable(text[pos:]))
    return "".join(out).replace("\n", "<br>")


def open_link(href: str, open_account: Callable[[str], None]) -> None:
    if href.startswith(ACCOUNT_LINK):
        open_account(href[len(ACCOUNT_LINK):])
    else:
        QDesktopServices.openUrl(QUrl(href))


class CaptionLabel(QLabel):
    """Word-wrapped label with a fixed width that always asks for the full height
    of its text. A plain QLabel's sizeHint guesses the wrap width, so long
    captions got too little height and were clipped at the bottom."""

    def sizeHint(self) -> QSize:
        return QSize(self.width(), self.heightForWidth(self.width()))

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()


def _header(username: str, account_dir: Path, on_author: Callable[[str], None]) -> QHBoxLayout:
    """Avatar and clickable @handle; callers append further widgets."""
    head = QHBoxLayout()
    head.setContentsMargins(8, 6, 8, 6)
    pic = QLabel()
    pic.setPixmap(avatar(account_dir / "profile.jpg", 32))
    pic.setFixedSize(32, 32)
    handle = QPushButton(f"@{username}")
    handle.setFlat(True)
    handle.setCursor(Qt.CursorShape.PointingHandCursor)
    handle.setStyleSheet("font-weight:600; text-align:left; padding:0 4px;")
    handle.clicked.connect(lambda: on_author(username))
    head.addWidget(pic)
    head.addWidget(handle)
    return head


def _caption(rich: str, on_account: Callable[[str], None]) -> CaptionLabel:
    cap = CaptionLabel(rich)
    cap.setTextFormat(Qt.TextFormat.RichText)
    cap.setWordWrap(True)
    cap.linkActivated.connect(lambda href: open_link(href, on_account))
    cap.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
    cap.setContentsMargins(10, 4, 10, 0)
    cap.setFixedWidth(WIDTH)
    return cap


def bio_html(biography: str, links: list[str], tracked: Callable[[str], bool]) -> str:
    parts = [caption_html(biography, tracked)] if biography else []
    parts += [f'<a href="{html.escape(url)}">{_breakable(url)}</a>' for url in links]
    return "<br>".join(parts)


class BioWidget(QFrame):
    """The profile bio, shown like a post (no date, no media) at the top of an account's feed."""
    authorClicked = Signal(str)

    def __init__(self, username: str, rich: str, account_dir: Path, parent=None):
        super().__init__(parent)
        self.rich = rich
        self.setObjectName("post")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFixedWidth(WIDTH + 2)
        head = _header(username, account_dir, self.authorClicked.emit)
        head.addStretch(1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 8)
        lay.setSpacing(4)
        lay.addLayout(head)
        lay.addWidget(_caption(rich, self.authorClicked.emit))


class PostWidget(QFrame):
    authorClicked = Signal(str)

    def __init__(self, post: Post, account_dir: Path, tracked: Callable[[str], bool], parent=None):
        super().__init__(parent)
        self.post = post
        self.setObjectName("post")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFixedWidth(WIDTH + 2)

        head = _header(post.username, account_dir, self.authorClicked.emit)
        when = QDateTime.fromSecsSinceEpoch(post.taken_at).toString("d MMMM yyyy  HH:mm")
        date = QLabel(when)
        date.setStyleSheet("color:#888;")
        if post.is_reel:
            reel = QLabel("Reel")
            reel.setStyleSheet("color:#888; font-size:11px; border:1px solid #888; border-radius:3px; padding:0 3px;")
            head.addWidget(reel)
        head.addStretch(1)
        head.addWidget(date)

        self.media = MediaViewer(post.media, account_dir)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 8)
        lay.setSpacing(4)
        lay.addLayout(head)
        lay.addWidget(self.media)
        if post.caption:
            lay.addWidget(_caption(caption_html(post.caption, tracked), self.authorClicked.emit))

    def load(self):
        self.media.load()

    def unload(self):
        self.media.unload()
