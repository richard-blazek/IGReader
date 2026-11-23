"""Carousel of a post's photos/videos, loaded lazily from the local cache."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QPoint, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QImageReader, QPainter, QPixmap, QPolygon
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSlider, QStackedLayout,
                               QVBoxLayout, QWidget)

from ..store import MediaItem

WIDTH = 560


def _aspect(item: MediaItem) -> float:
    """Frame height/width: 1.91:1 .. 4:5 like Instagram's feed. Taller media (reels) is
    letterboxed inside the frame, never cropped."""
    if not item.width or not item.height:
        return 1.0
    return max(1 / 1.91, min(1.25, item.height / item.width))


def load_scaled(path: Path, size: QSize) -> Optional[QPixmap]:
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    src = reader.size()
    if not src.isValid():
        return None
    reader.setScaledSize(src.scaled(size, Qt.AspectRatioMode.KeepAspectRatio))
    img = reader.read()
    if img.isNull():
        return None
    # letterbox into the frame (show the whole photo, never crop)
    out = QPixmap(size)
    out.fill(QColor("#111"))
    p = QPainter(out)
    p.drawImage((size.width() - img.width()) // 2, (size.height() - img.height()) // 2, img)
    p.end()
    return out


def _play_overlay(pm: QPixmap) -> QPixmap:
    out = QPixmap(pm)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    c = out.rect().center()
    r = 34
    p.setBrush(QColor(0, 0, 0, 140))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(c, r, r)
    p.setBrush(QColor(255, 255, 255, 230))
    p.drawPolygon(QPolygon([QPoint(c.x() - 11, c.y() - 16), QPoint(c.x() - 11, c.y() + 16), QPoint(c.x() + 17, c.y())]))
    p.end()
    return out


class ClickLabel(QLabel):
    clicked = Signal()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(e)


class VideoPlayer(QWidget):
    """Created on demand when a video is played; one active player app-wide."""
    active: Optional["VideoPlayer"] = None

    def __init__(self, path: Path, size: QSize, parent=None):
        super().__init__(parent)
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        from PySide6.QtMultimediaWidgets import QVideoWidget

        self.video = QVideoWidget(self)
        self.video.setFixedSize(size)
        self.audio = QAudioOutput(self)
        self.audio.setMuted(True)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.setLoops(QMediaPlayer.Loops.Infinite)

        self.play_btn = QPushButton("⏸")
        self.mute_btn = QPushButton("🔇 Unmute")
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.time = QLabel("0:00")
        for b in (self.play_btn, self.mute_btn):
            b.setFlat(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
        self.play_btn.clicked.connect(self.toggle)
        self.mute_btn.clicked.connect(self.toggle_mute)
        self.slider.sliderMoved.connect(self.player.setPosition)
        self.player.durationChanged.connect(lambda d: self.slider.setRange(0, d))
        self.player.positionChanged.connect(self._pos)

        bar = QHBoxLayout()
        bar.setContentsMargins(4, 0, 4, 0)
        bar.addWidget(self.play_btn)
        bar.addWidget(self.slider, 1)
        bar.addWidget(self.time)
        bar.addWidget(self.mute_btn)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.video)
        lay.addLayout(bar)
        self.video.mousePressEvent = lambda e: self.toggle()

    def start(self):
        if VideoPlayer.active is not None and VideoPlayer.active is not self:
            VideoPlayer.active.player.pause()
            VideoPlayer.active.play_btn.setText("▶")
        VideoPlayer.active = self
        self.player.play()
        self.play_btn.setText("⏸")

    def toggle(self):
        from PySide6.QtMultimedia import QMediaPlayer
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            self.play_btn.setText("▶")
        else:
            self.start()

    def toggle_mute(self):
        m = not self.audio.isMuted()
        self.audio.setMuted(m)
        self.mute_btn.setText("🔇 Unmute" if m else "🔊 Mute")

    def _pos(self, ms: int):
        if not self.slider.isSliderDown():
            self.slider.setValue(ms)
        s = ms // 1000
        self.time.setText(f"{s // 60}:{s % 60:02d}")

    def release(self):
        self.player.stop()
        self.player.setSource(QUrl())
        if VideoPlayer.active is self:
            VideoPlayer.active = None


class MediaViewer(QWidget):
    def __init__(self, items: list[MediaItem], account_dir: Path, parent=None):
        super().__init__(parent)
        self.items = items
        self.dir = account_dir
        self.idx = 0
        self.loaded = False
        self.player: Optional[VideoPlayer] = None
        a = _aspect(items[0]) if items else 1.0
        self.frame = QSize(WIDTH, int(WIDTH * a))

        self.stack = QStackedLayout()
        self.image = ClickLabel()
        self.image.setFixedSize(self.frame)
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setStyleSheet("background:#111; color:#888;")
        self.image.clicked.connect(self._clicked)
        self.stack.addWidget(self.image)
        self.holder = holder = QWidget()
        holder.setLayout(self.stack)
        holder.setFixedSize(self.frame)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addWidget(holder)

        if len(items) > 1:
            nav = QHBoxLayout()
            self.prev = QPushButton("◀")
            self.next = QPushButton("▶")
            self.counter = QLabel()
            self.counter.setAlignment(Qt.AlignmentFlag.AlignCenter)
            for b in (self.prev, self.next):
                b.setFixedWidth(40)
                b.setFlat(True)
                b.setCursor(Qt.CursorShape.PointingHandCursor)
            self.prev.clicked.connect(lambda: self.show_index(self.idx - 1))
            self.next.clicked.connect(lambda: self.show_index(self.idx + 1))
            nav.addWidget(self.prev)
            nav.addWidget(self.counter, 1)
            nav.addWidget(self.next)
            lay.addLayout(nav)
        else:
            self.counter = None
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._update_nav()

    def _update_nav(self):
        if self.counter is not None:
            kinds = "".join("●" if i == self.idx else "○" for i in range(len(self.items)))
            self.counter.setText(f"{kinds}   {self.idx + 1}/{len(self.items)}")
            self.prev.setEnabled(self.idx > 0)
            self.next.setEnabled(self.idx < len(self.items) - 1)

    def show_index(self, i: int):
        if not 0 <= i < len(self.items):
            return
        self._drop_player()
        self.idx = i
        self._update_nav()
        if self.loaded:
            self._render()

    def _render(self):
        item = self.items[self.idx]
        src = item.thumb if item.kind == "video" else item.file
        pm = load_scaled(self.dir / src, self.frame) if src else None
        if pm is None:
            self.image.setPixmap(QPixmap())
            self.image.setText("▶ video" if item.kind == "video" else "image missing")
            return
        if item.kind == "video":
            pm = _play_overlay(pm)
        self.image.setCursor(Qt.CursorShape.PointingHandCursor if item.kind == "video" else Qt.CursorShape.ArrowCursor)
        self.image.setPixmap(pm)

    def _clicked(self):
        item = self.items[self.idx]
        if item.kind != "video":
            return
        path = self.dir / item.file
        if not path.exists():
            return
        self.player = VideoPlayer(path, self.frame)
        self.stack.addWidget(self.player)
        self.stack.setCurrentWidget(self.player)
        self.holder.setFixedHeight(self.player.sizeHint().height())
        self.player.start()

    def _drop_player(self):
        if self.player is not None:
            self.player.release()
            self.stack.removeWidget(self.player)
            self.player.deleteLater()
            self.player = None
            self.stack.setCurrentWidget(self.image)
            self.holder.setFixedHeight(self.frame.height())

    def load(self):
        if not self.loaded:
            self.loaded = True
            self._render()

    def unload(self):
        if self.loaded:
            self.loaded = False
            self._drop_player()
            self.image.setPixmap(QPixmap())
