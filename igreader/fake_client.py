"""Offline stand-in for AnonClient (IGREADER_FAKE=1). Generates deterministic accounts/posts.

Usernames: any name works. The post count comes from a trailing number if there is one
("demo40" -> 40 posts), otherwise from a hash. "private_*" are private and "missing_*" don't exist.
IGREADER_FAKE_FAIL=N makes the N-th API call fail with a rate-limit error.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .ig_client import IGError, Page, ProfileInfo, RawMedia, RawPost, Stub
from .throttle import Throttle

BASE_TS = 1_764_000_000  # Nov 2025
PAGE = 12


def _h(s: str) -> int:
    return int(hashlib.sha1(s.encode()).hexdigest(), 16)


class FakeClient:
    def __init__(self, throttle: Throttle, workdir: Path):
        self.throttle = throttle
        self.workdir = workdir
        self.api_calls = 0
        self.fail_at = int(os.environ.get("IGREADER_FAKE_FAIL", "0") or 0)
        self._video: Optional[Path] = None
        self.extra: dict[str, int] = {}  # tests: add N newer posts on top
        self.pinned_k = (5, 9)
        self.downloads = 0
        self.fail_download_at = 0  # tests: make the N-th download fail
        self._fresh_first_page: set[str] = set()

    # --- deterministic data --------------------------------------------------
    def _count(self, username: str) -> int:
        m = re.search(r"(\d+)$", username)
        return int(m.group(1)) if m else 5 + _h(username) % 70

    def _timeline(self, username: str) -> list[RawPost]:
        """All posts, newest first. Post k (1 = oldest) is stable when new posts are added on
        top via self.extra[username]."""
        n = self._count(username) + self.extra.get(username, 0)
        h = _h(username)
        posts = []
        ts = BASE_TS - 86400 * 400 + h % 86400
        for k in range(1, n + 1):
            ts += 3600 * (2 + (_h(f"{username}{k}") % 96))
            sc = f"{username}.{k:04d}"
            kind = k % 7
            if kind == 3:
                media = [RawMedia("image" if j != 1 else "video", f"fake://{sc}/{j}", f"fake://{sc}/{j}t", 1080, 1350)
                         for j in range(3)]
            elif kind == 5:
                media = [RawMedia("video", f"fake://{sc}/0", f"fake://{sc}/0t", 1080, 1920)]
            else:
                media = [RawMedia("image", f"fake://{sc}/0", None, 1080, 1080 + (k % 3) * 135)]
            caption = (f"Post {k} of @{username} 🌄\nGenerated offline. "
                       f"See https://example.com/{k} #fake #igreader") if k % 4 else ""
            posts.append(RawPost(sc, ts, caption, False, kind == 5, media))
        posts.reverse()
        return posts

    def _grid(self, username):
        grid = self._timeline(username)
        if self._count(username) > 25:  # pin old posts (#5 and #9 by default) to the top
            for k in self.pinned_k:
                idx = next((i for i, p in enumerate(grid) if p.shortcode.endswith(f".{k:04d}")), None)
                if idx is not None:
                    grid.insert(0, grid.pop(idx))
        if username.startswith("odd"):
            # one specific post listed out of date order, a few places too low (as Instagram sometimes does)
            k = self._count(username) - 2
            sc = f"{username}.{k:04d}"
            idx = next(i for i, p in enumerate(grid) if p.shortcode == sc)
            post = grid.pop(idx)
            after = next(i for i, p in enumerate(grid) if p.shortcode == f"{username}.{k - 5:04d}")
            grid.insert(after + 1, post)
        return grid

    def _tick(self):
        self.throttle.before_api()
        self.api_calls += 1
        if self.fail_at and self.api_calls == self.fail_at:
            raise IGError("block", "Fake: Please wait a few minutes before you try again.")

    # --- Client protocol -----------------------------------------------------
    def resolve_profile(self, username: str) -> ProfileInfo:
        self._tick()
        if username.startswith("missing"):
            raise IGError("account", f"Profile {username} does not exist.")
        uid = str(_h(username) % 10**10)
        self._fresh_first_page.add(username)  # like the real profile page, it embeds page 1
        return ProfileInfo(username, uid, username.replace("_", " ").title(),
                           username.startswith("private"), f"fake://avatar/{username}", page_id="p" + uid)

    def posts_page(self, acc, cursor) -> Page:
        """Like the logged-out API: only shortcodes, no metadata, no pin flags."""
        if cursor is not None or acc.username not in self._fresh_first_page:
            self._tick()  # first page = profile page request; reused right after resolve_profile
        self._fresh_first_page.discard(acc.username)
        grid = self._grid(acc.username)
        # like Instagram's cursors: "continue after this post", not a numeric offset
        start = 0 if cursor is None else next(i for i, p in enumerate(grid) if p.shortcode == cursor) + 1
        chunk = grid[start:start + PAGE]
        more = start + PAGE < len(grid)
        return Page([Stub(p.shortcode) for p in chunk], chunk[-1].shortcode if more else None, more)

    def fetch_post(self, shortcode: str) -> RawPost:
        self._tick()
        if int(shortcode.rsplit(".", 1)[1]) % 23 == 0:  # like an age-gated post
            raise IGError("unavailable", f"Post {shortcode} is not viewable logged out")
        username = shortcode.rsplit(".", 1)[0]
        for p in self._timeline(username):
            if p.shortcode == shortcode:
                return p
        raise IGError("other", f"Unknown fake post {shortcode}")

    def download(self, url: str, dest: Path) -> None:
        self.throttle.before_media()
        self.downloads += 1
        if self.fail_download_at and self.downloads == self.fail_download_at:
            raise IGError("network", "Fake: connection reset")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.suffix == ".mp4":
            src = self._fake_video()
            if src:
                shutil.copyfile(src, dest)
            else:
                dest.write_bytes(b"")
            return
        self._fake_image(url, dest)

    def _fake_image(self, label: str, dest: Path) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter
        h = _h(label)
        w, ht = (320, 320) if "avatar" in label else (1080, 1080)
        img = QImage(w, ht, QImage.Format.Format_RGB32)
        img.fill(QColor.fromHsv(h % 360, 120, 200))
        p = QPainter(img)
        p.fillRect(w // 8, ht // 8, w * 3 // 4, ht * 3 // 4, QColor.fromHsv((h // 7) % 360, 160, 140))
        if QGuiApplication.instance() is not None:
            p.setPen(Qt.GlobalColor.white)
            f = QFont()
            f.setPixelSize(w // 12)
            p.setFont(f)
            p.drawText(img.rect(), Qt.AlignmentFlag.AlignCenter, label.replace("fake://", ""))
        p.end()
        tmp = dest.with_name(dest.name + ".part.jpg")
        img.save(str(tmp), "JPG", 85)
        os.replace(tmp, dest)

    def _fake_video(self) -> Optional[Path]:
        if self._video is not None:
            return self._video if self._video.exists() else None
        out = self.workdir / "fake_sample.mp4"
        if not out.exists() and shutil.which("ffmpeg"):
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=4:size=480x600:rate=25",
                 "-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-shortest",
                 "-c:v", "mpeg4", "-q:v", "5", "-pix_fmt", "yuv420p", "-c:a", "aac", str(out)],
                check=False)
        self._video = out  # tried once; if it failed, videos get an empty placeholder
        return out if out.exists() else None
