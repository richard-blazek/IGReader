"""Plain-text (JSON) storage of accounts, posts and app state, plus an in-memory index.

Layout under the data dir:
    state.json                       global state: cooldown, settings
    accounts.json                    ordered list of followed usernames
    accounts/<username>/account.json per-account fetch state
    accounts/<username>/profile.jpg
    accounts/<username>/posts/<shortcode>.json
    accounts/<username>/media/<shortcode>_<idx>.<ext>
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

STREAMS = ("posts",)  # profile grid only (the Reels tab needs a login)


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _ts(iso: Optional[str]) -> Optional[int]:
    if not iso:
        return None
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def write_json(path: Path, data: Any) -> None:
    """Atomically write pretty-printed UTF-8 JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


@dataclass
class MediaItem:
    idx: int
    kind: str  # "image" | "video"
    file: str  # path relative to the account dir
    thumb: Optional[str] = None  # video cover image, relative to the account dir
    width: Optional[int] = None
    height: Optional[int] = None


@dataclass
class Post:
    shortcode: str
    username: str
    taken_at: int  # unix seconds (stored as ISO 8601 UTC in JSON)
    caption: str = ""
    is_reel: bool = False
    fetched_at: int = 0
    media: list[MediaItem] = field(default_factory=list)

    def to_json(self) -> dict:
        d = asdict(self)
        d["taken_at"] = _iso(self.taken_at)
        d["fetched_at"] = _iso(self.fetched_at)
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Post":
        return cls(
            shortcode=d["shortcode"],
            username=d["username"],
            taken_at=_ts(d["taken_at"]) or 0,
            caption=d.get("caption") or "",
            is_reel=bool(d.get("is_reel")),
            fetched_at=_ts(d.get("fetched_at")) or 0,
            media=[MediaItem(**m) for m in d.get("media", [])],
        )


@dataclass
class StreamState:
    """Pagination state of one timeline of an account (currently only the profile grid)."""
    started: bool = False  # first page has been fetched
    complete: bool = False  # reached the very oldest item
    cursor: Optional[str] = None  # end_cursor for the next older page
    oldest_ts: Optional[int] = None  # oldest date-ordered item seen walking down from the top
    newest_ts: Optional[int] = None  # newest date-ordered item; refresh walks down to here


@dataclass
class Account:
    username: str
    user_id: str = ""
    page_id: str = ""  # logged-out profile page id, used for pagination
    full_name: str = ""
    added_at: int = 0
    last_refresh_at: Optional[int] = None
    last_error: Optional[str] = None
    skipped: list[str] = field(default_factory=list)  # posts not viewable logged out (gated)
    # Profile timeline order, as Instagram lists it: `head` = posts at the top that are out of
    # date order (pinned), `order` = the rest, contiguous from the top down to the cached end.
    head: list[str] = field(default_factory=list)
    order: list[str] = field(default_factory=list)
    streams: dict[str, StreamState] = field(default_factory=lambda: {s: StreamState() for s in STREAMS})

    def to_json(self) -> dict:
        return {
            "username": self.username,
            "user_id": self.user_id,
            "page_id": self.page_id,
            "full_name": self.full_name,
            "added_at": _iso(self.added_at),
            "last_refresh_at": _iso(self.last_refresh_at),
            "last_error": self.last_error,
            "skipped": self.skipped,
            "head": self.head,
            "order": self.order,
            "streams": {k: asdict(v) for k, v in self.streams.items()},
        }

    @classmethod
    def from_json(cls, d: dict) -> "Account":
        streams = {s: StreamState() for s in STREAMS}
        for k, v in (d.get("streams") or {}).items():
            if k in streams:
                streams[k] = StreamState(**v)
        return cls(
            username=d["username"],
            user_id=str(d.get("user_id") or ""),
            page_id=str(d.get("page_id") or ""),
            full_name=d.get("full_name") or "",
            added_at=_ts(d.get("added_at")) or 0,
            last_refresh_at=_ts(d.get("last_refresh_at")),
            last_error=d.get("last_error"),
            skipped=list(d.get("skipped") or []),
            head=list(d.get("head") or []),
            order=list(d.get("order") or []),
            streams=streams,
        )

    # --- history frontier -------------------------------------------------
    def frontier(self) -> Optional[int]:
        """Posts with taken_at >= frontier are known to be complete for this account.

        None means no constraint (full history cached, or nothing started yet)."""
        values = [s.oldest_ts for s in self.streams.values()
                  if s.started and not s.complete and s.oldest_ts is not None]
        return max(values) if values else None

    def next_stream(self) -> Optional[str]:
        """Which stream to fetch an older page of, to push the frontier down."""
        for name, s in self.streams.items():
            if not s.started:
                return name
        incomplete = [(s.oldest_ts if s.oldest_ts is not None else 2**62, name)
                      for name, s in self.streams.items() if not s.complete]
        return max(incomplete)[1] if incomplete else None

    @property
    def history_complete(self) -> bool:
        return all(s.complete for s in self.streams.values())


DEFAULT_SETTINGS = {
    "refresh_hours": 6,
    "user_agent": None,  # None = instaloader's default desktop UA
}


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.RLock()
        self._accounts: dict[str, Account] = {}
        self._order: list[str] = []
        self._posts: dict[str, Post] = {}
        self.state: dict = {}
        self.load()

    # --- paths --------------------------------------------------------------
    def account_dir(self, username: str) -> Path:
        return self.root / "accounts" / username

    def media_path(self, post: Post, rel: str) -> Path:
        return self.account_dir(post.username) / rel

    def profile_pic(self, username: str) -> Path:
        return self.account_dir(username) / "profile.jpg"

    # --- loading ------------------------------------------------------------
    def load(self) -> None:
        with self.lock:
            st = read_json(self.root / "state.json", {}) or {}
            st["settings"] = {**DEFAULT_SETTINGS, **(st.get("settings") or {})}
            self.state = st
            self._order = list(read_json(self.root / "accounts.json", []) or [])
            self._accounts.clear()
            self._posts.clear()
            for u in list(self._order):
                d = read_json(self.account_dir(u) / "account.json")
                if d is None:
                    self._order.remove(u)
                    continue
                self._accounts[u] = Account.from_json(d)
                for p in (self.account_dir(u) / "posts").glob("*.json"):
                    try:
                        post = Post.from_json(read_json(p))
                    except (ValueError, KeyError, TypeError):
                        continue  # skip a corrupt file rather than refusing to start
                    self._posts[post.shortcode] = post
            for acc in self._accounts.values():  # data from before the profile order was stored
                if not acc.head and not acc.order:
                    floor = acc.frontier()
                    mine = [q for q in self._posts.values() if q.username == acc.username
                            and (floor is None or q.taken_at >= floor)]
                    acc.order = [q.shortcode for q in sorted(mine, key=lambda q: q.taken_at, reverse=True)]

    def save_state(self) -> None:
        with self.lock:
            write_json(self.root / "state.json", self.state)

    @property
    def settings(self) -> dict:
        return self.state["settings"]

    # --- cooldown -----------------------------------------------------------
    COOLDOWNS = (3600, 6 * 3600, 24 * 3600)

    def cooldown_until(self) -> Optional[int]:
        until = _ts(self.state.get("cooldown_until"))
        return until if until and until > time.time() else None

    def start_cooldown(self, reason: str) -> int:
        with self.lock:
            level = min(int(self.state.get("cooldown_level", 0)), len(self.COOLDOWNS) - 1)
            until = int(time.time()) + self.COOLDOWNS[level]
            self.state["cooldown_level"] = min(level + 1, len(self.COOLDOWNS) - 1)
            self.state["cooldown_until"] = _iso(until)
            self.state["cooldown_reason"] = reason
            self.save_state()
            return until

    def clear_cooldown_level(self) -> None:
        """Called after a run finished without any pushback from Instagram."""
        with self.lock:
            if self.state.get("cooldown_level") or self.state.get("cooldown_until"):
                self.state["cooldown_level"] = 0
                self.state["cooldown_until"] = None
                self.state["cooldown_reason"] = None
                self.save_state()

    # --- accounts -----------------------------------------------------------
    def accounts(self) -> list[Account]:
        with self.lock:
            return [self._accounts[u] for u in self._order]

    def account(self, username: str) -> Optional[Account]:
        with self.lock:
            return self._accounts.get(username)

    def add_account(self, acc: Account) -> None:
        with self.lock:
            if acc.username not in self._accounts:
                self._order.append(acc.username)
            self._accounts[acc.username] = acc
            self.save_account(acc)
            write_json(self.root / "accounts.json", self._order)

    def save_account(self, acc: Account) -> None:
        with self.lock:
            if acc.username in self._accounts:
                write_json(self.account_dir(acc.username) / "account.json", acc.to_json())

    def remove_account(self, username: str) -> None:
        with self.lock:
            self._accounts.pop(username, None)
            if username in self._order:
                self._order.remove(username)
            write_json(self.root / "accounts.json", self._order)
            for sc in [sc for sc, p in self._posts.items() if p.username == username]:
                del self._posts[sc]
            shutil.rmtree(self.account_dir(username), ignore_errors=True)

    # --- posts --------------------------------------------------------------
    def has_post(self, shortcode: str) -> bool:
        with self.lock:
            return shortcode in self._posts

    def get_post(self, shortcode: str) -> Optional[Post]:
        with self.lock:
            return self._posts.get(shortcode)

    def save_post(self, post: Post) -> bool:
        """Persist a post. Returns False if its account was removed meanwhile."""
        with self.lock:
            if post.username not in self._accounts:
                return False
            write_json(self.account_dir(post.username) / "posts" / f"{post.shortcode}.json", post.to_json())
            self._posts[post.shortcode] = post
            return True

    def account_posts(self, username: str) -> list[Post]:
        with self.lock:
            return [p for p in self._posts.values() if p.username == username]

    # --- views --------------------------------------------------------------
    def feed(self, username: Optional[str]) -> tuple[list[Post], bool]:
        """Posts to display, newest first, and whether the full history is shown.

        Only posts down to the cache frontier are returned, so the list is never missing
        posts that exist on Instagram but haven't been fetched yet."""
        with self.lock:
            if username is None:
                accs = [self._accounts[u] for u in self._order]
                frontiers = [f for f in (a.frontier() for a in accs) if f is not None]
                frontier = max(frontiers) if frontiers else None
                posts = [p for p in self._posts.values() if p.username in self._accounts]
                visible = [p for p in posts if frontier is None or p.taken_at >= frontier]
                visible.sort(key=lambda p: (p.taken_at, p.shortcode), reverse=True)
                complete = all(a.history_complete for a in accs)
                return visible, complete
            acc = self._accounts.get(username)
            if acc is None:
                return [], True
            seen: set[str] = set()
            out = []
            for sc in acc.head + acc.order:  # exactly the profile's order
                p = self._posts.get(sc)
                if p is not None and sc not in seen:
                    seen.add(sc)
                    out.append(p)
            return out, acc.history_complete

    def limiting_account(self) -> Optional[str]:
        """In the merged feed: the account whose history must be extended to show more."""
        with self.lock:
            best, best_key = None, None
            for a in self.accounts():
                if a.history_complete:
                    continue
                if any(not s.started for s in a.streams.values()):
                    return a.username
                f = a.frontier()
                key = f if f is not None else -1
                if best_key is None or key > best_key:
                    best, best_key = a.username, key
            return best
