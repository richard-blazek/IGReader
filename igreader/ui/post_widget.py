"""One post: author, date & time, media carousel, caption."""
from __future__ import annotations

import html
import re
from pathlib import Path

from PySide6.QtCore import QDateTime, QSize, Qt, Signal
from PySide6.QtGui import QBrush, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from ..store import Post
from .media_viewer import WIDTH, MediaViewer

_avatar_cache: dict[tuple[str, int], QPixmap] = {}

URL_RE = re.compile(r"(https?://[^\s<]+)|(?<![\w@])@([A-Za-z0-9._]{1,30})|(?<![\w#])#(\w+)")


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


def caption_html(text: str) -> str:
    out, pos = [], 0
    for m in URL_RE.finditer(text):
        out.append(_breakable(text[pos:m.start()]))
        url, user, tag = m.groups()
        if url:
            out.append(f'<a href="{html.escape(url)}">{_breakable(url)}</a>')
        elif user:
            out.append(f'<a href="https://www.instagram.com/{user}/">@{html.escape(user)}</a>')
        else:
            out.append(f'<a href="https://www.instagram.com/explore/tags/{html.escape(tag)}/">#{html.escape(tag)}</a>')
        pos = m.end()
    out.append(_breakable(text[pos:]))
    return "".join(out).replace("\n", "<br>")


class CaptionLabel(QLabel):
    """Word-wrapped label with a fixed width that always asks for the full height
    of its text. A plain QLabel's sizeHint guesses the wrap width, so long
    captions got too little height and were clipped at the bottom."""

    def sizeHint(self) -> QSize:
        return QSize(self.width(), self.heightForWidth(self.width()))

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()


class PostWidget(QFrame):
    authorClicked = Signal(str)

    def __init__(self, post: Post, account_dir: Path, parent=None):
        super().__init__(parent)
        self.post = post
        self.setObjectName("post")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFixedWidth(WIDTH + 2)

        head = QHBoxLayout()
        head.setContentsMargins(8, 6, 8, 6)
        pic = QLabel()
        pic.setPixmap(avatar(account_dir / "profile.jpg", 32))
        pic.setFixedSize(32, 32)
        handle = QPushButton(f"@{post.username}")
        handle.setFlat(True)
        handle.setCursor(Qt.CursorShape.PointingHandCursor)
        handle.setStyleSheet("font-weight:600; text-align:left; padding:0 4px;")
        handle.clicked.connect(lambda: self.authorClicked.emit(post.username))
        when = QDateTime.fromSecsSinceEpoch(post.taken_at).toString("yyyy-MM-dd  HH:mm")
        date = QLabel(when)
        date.setStyleSheet("color:#888;")
        head.addWidget(pic)
        head.addWidget(handle)
        if post.is_reel:
            reel = QLabel("Reel")
            reel.setStyleSheet("color:#888; font-size:11px; border:1px solid #888; border-radius:3px; padding:0 3px;")
            head.addWidget(reel)
        head.addStretch(1)
        head.addWidget(date)
        kind = "reel" if post.is_reel else "p"
        link = QLabel(f'<a href="https://www.instagram.com/{kind}/{post.shortcode}/">↗</a>')
        link.setOpenExternalLinks(True)
        link.setToolTip("Open on Instagram")
        head.addWidget(link)

        self.media = MediaViewer(post.media, account_dir)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 8)
        lay.setSpacing(4)
        lay.addLayout(head)
        lay.addWidget(self.media)
        if post.caption:
            cap = CaptionLabel(caption_html(post.caption))
            cap.setTextFormat(Qt.TextFormat.RichText)
            cap.setWordWrap(True)
            cap.setOpenExternalLinks(True)
            cap.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
            cap.setContentsMargins(10, 4, 10, 0)
            cap.setFixedWidth(WIDTH)
            lay.addWidget(cap)

    def load(self):
        self.media.load()

    def unload(self):
        self.media.unload()
