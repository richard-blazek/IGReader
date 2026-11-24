"""Fetch logic (no Qt): add account, refresh newest posts, extend history by one page.

Invariants that keep feeds correct and requests minimal:
* A post that is already stored is never requested or downloaded again.
* Everything between the stream's `oldest_ts` and `newest_ts` is cached. History is
  extended only via the stored cursor; refresh walks from the top down to `newest_ts`, and
  `newest_ts` only advances once a refresh completed, so an interrupted refresh is simply
  redone (already-saved posts cost nothing).
* Stream state is persisted only after a page has been fully processed.
* Each account keeps the profile's own order (`head` + `order`) for display. Logged-out
  timeline nodes don't say which posts are pinned; pinned posts are at the top of the
  first page and older than what follows them. That "head" is excluded from the date
  logic (frontier, refresh stop) but shown first, exactly as on the profile.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from .ig_client import Client, IGError, Page, RawPost, Stub
from .store import Account, MediaItem, Post, Store

log = logging.getLogger("igreader.fetch")

STREAM = "posts"
REFRESH_MAX_PAGES = 30
MAX_PINNED = 3


class Events(Protocol):
    def posts_changed(self, username: str) -> None: ...
    def accounts_changed(self) -> None: ...


class AccountGone(Exception):
    """The account was removed while a job for it was running."""


@dataclass
class Resolved:
    shortcode: str
    ts: int
    out_of_order: bool  # pinned -> not part of the date-ordered timeline


class Fetcher:
    def __init__(self, store: Store, client: Client, events: Events,
                 status: Callable[[str], None] = lambda s: None):
        self.store = store
        self.client = client
        self.events = events
        self.status = status

    # --- helpers -------------------------------------------------------------
    def _live(self, username: str) -> Account:
        acc = self.store.account(username)
        if acc is None:
            raise AccountGone(username)
        return acc

    def _process_page(self, acc: Account, page: Page, first_page: bool) -> list[Resolved]:
        """Resolve every item (metadata request only for unknown stubs) and save new posts.
        On the first page, the leading out-of-date-order items (pinned) are marked."""
        out: list[Resolved] = []
        for n, item in enumerate(page.items, 1):
            if item.shortcode in acc.skipped:
                continue
            known = self.store.get_post(item.shortcode)
            if isinstance(item, RawPost):
                raw = None if known else item
                r = Resolved(item.shortcode, item.taken_at, item.out_of_order)
            elif known is not None:
                raw = None
                r = Resolved(item.shortcode, known.taken_at, item.out_of_order)
            else:
                self.status(f"@{acc.username}: loading post {n}/{len(page.items)}…")
                try:
                    raw = self.client.fetch_post(item.shortcode)
                except IGError as e:
                    if e.kind != "unavailable":
                        raise
                    log.info("@%s: skipping %s: %s", acc.username, item.shortcode, e)
                    acc = self._live(acc.username)
                    acc.skipped.append(item.shortcode)
                    self.store.save_account(acc)
                    continue
                r = Resolved(raw.shortcode, raw.taken_at, raw.out_of_order or item.out_of_order)
            if raw is not None:
                self._save(acc.username, raw)
            out.append(r)
        if first_page:
            last = -1
            for i, r in enumerate(out[:MAX_PINNED]):
                later = out[i + 1:]
                if r.out_of_order or (later and r.ts < max(x.ts for x in later)):
                    last = i
            for r in out[:last + 1]:  # pinned posts form a prefix of the first page
                r.out_of_order = True
        return out

    @staticmethod
    def _split_head(res: list[Resolved]) -> tuple[list[Resolved], list[Resolved]]:
        k = 0
        while k < len(res) and res[k].out_of_order:
            k += 1
        return res[:k], res[k:]

    def _place(self, acc: Account, sc: str) -> None:
        """Insert a known post into `order` by date, if it lies within the cached range."""
        p = self.store.get_post(sc)
        s = acc.streams[STREAM]
        if p is None or sc in acc.order or sc in acc.head:
            return
        if not s.complete and (s.oldest_ts is None or p.taken_at < s.oldest_ts):
            return  # below the cached range: it is appended when history gets there
        for i, other in enumerate(acc.order):
            q = self.store.get_post(other)
            if q is not None and q.taken_at < p.taken_at:
                acc.order.insert(i, sc)
                return
        acc.order.append(sc)

    def _save(self, username: str, raw: RawPost) -> None:
        adir = self.store.account_dir(username)
        media = []
        for i, m in enumerate(raw.media):
            ext = ".mp4" if m.kind == "video" else ".jpg"
            rel = f"media/{raw.shortcode}_{i}{ext}"
            if not (adir / rel).exists():
                self._live(username)
                self.client.download(m.url, adir / rel)
            thumb = None
            if m.kind == "video" and m.thumb_url:
                thumb = f"media/{raw.shortcode}_{i}_cover.jpg"
                if not (adir / thumb).exists():
                    self.client.download(m.thumb_url, adir / thumb)
            media.append(MediaItem(i, m.kind, rel, thumb, m.width, m.height))
        post = Post(raw.shortcode, username, raw.taken_at, raw.caption, raw.is_reel,
                    int(time.time()), media)
        if not self.store.save_post(post):
            raise AccountGone(username)
        self.events.posts_changed(username)

    # --- operations ----------------------------------------------------------
    def add_account(self, username: str) -> Account:
        username = username.strip().lstrip("@").lower()
        if self.store.account(username):
            raise IGError("account", f"@{username} is already in the list")
        info = self.client.resolve_profile(username)
        if info.is_private:
            raise IGError("account", f"@{info.username} is private")
        acc = Account(info.username, user_id=info.user_id, page_id=info.page_id,
                      full_name=info.full_name, biography=info.biography, bio_links=info.bio_links,
                      added_at=int(time.time()))
        self.store.add_account(acc)
        self.events.accounts_changed()
        if info.pic_url:
            try:
                self.client.download(info.pic_url, self.store.profile_pic(acc.username))
                self.events.accounts_changed()
            except IGError as e:
                if e.kind == "block":
                    raise
                log.warning("profile picture: %s", e)
        self.extend_history(acc.username)  # first page comes from the profile page just loaded
        acc = self._live(acc.username)
        acc.last_refresh_at = int(time.time())
        self.store.save_account(acc)
        return acc

    def extend_history(self, username: str) -> bool:
        """Fetch one older page (more if a page held only pinned posts). Returns False if
        there was nothing left to fetch."""
        acc = self._live(username)
        s = acc.streams[STREAM]
        if s.complete:
            return False
        for _ in range(3):
            first = not s.started
            self.status(f"@{username}: loading {'older ' if not first else ''}posts…")
            page = self.client.posts_page(acc, s.cursor if s.started else None)
            res = self._process_page(acc, page, first)
            acc = self._live(username)
            if first:
                head, body = self._split_head(res)
                acc.head, acc.order = [r.shortcode for r in head], []
            else:
                body = res
            present = set(acc.head) | set(acc.order)
            for r in body:
                if r.shortcode not in present:
                    acc.order.append(r.shortcode)
                    present.add(r.shortcode)
            in_order = [r.ts for r in body if not r.out_of_order]
            s = acc.streams[STREAM]
            s.started = True
            s.cursor = page.next_cursor
            s.complete = not page.has_next or not page.next_cursor
            if in_order:
                s.oldest_ts = min(in_order + ([s.oldest_ts] if s.oldest_ts is not None else []))
                s.newest_ts = max(in_order + ([s.newest_ts] if s.newest_ts is not None else []))
            self.store.save_account(acc)
            self.events.posts_changed(username)
            if in_order or s.complete:
                break
        return True

    def refresh(self, username: str) -> int:
        """Fetch posts newer than the cache. Returns number of new posts."""
        acc = self._live(username)
        before = len(self.store.account_posts(username))
        if not acc.streams[STREAM].started:
            self.extend_history(username)
        else:
            self._refresh_stream(acc)
        acc = self._live(username)
        acc.last_refresh_at = int(time.time())
        acc.last_error = None
        self.store.save_account(acc)
        self.events.accounts_changed()
        return len(self.store.account_posts(username)) - before

    def _refresh_stream(self, acc: Account) -> None:
        username = acc.username
        top = acc.streams[STREAM].newest_ts
        new_ts: list[int] = []
        prefix: list[str] = []  # new date-ordered posts, in profile order
        stray: list[str] = []  # page-1 posts below the reached point that aren't placed yet
        new_head: list[str] = []
        cursor = None
        reached = False
        last_page: Optional[Page] = None
        for n in range(REFRESH_MAX_PAGES):
            self.status(f"@{username}: checking for new posts" + (f" (page {n + 1})" if n else "…"))
            page = last_page = self.client.posts_page(acc, cursor)
            res = self._process_page(acc, page, first_page=n == 0)
            if n == 0:
                head, body = self._split_head(res)
                new_head = [r.shortcode for r in head]
            else:
                body = res
            for i, r in enumerate(body):
                if top is not None and r.ts <= top and not r.out_of_order:
                    reached = True
                    stray = [x.shortcode for x in body[i:]]
                    break
                prefix.append(r.shortcode)
                if not r.out_of_order:
                    new_ts.append(r.ts)
            if reached or not page.has_next or not page.next_cursor:
                break
            cursor = page.next_cursor

        acc = self._live(username)
        s = acc.streams[STREAM]
        old_head = acc.head
        acc.head = new_head
        if new_ts:
            s.newest_ts = max(new_ts + ([s.newest_ts] if s.newest_ts is not None else []))
        if reached:
            taken = set(prefix) | set(new_head)
            acc.order = prefix + [x for x in acc.order if x not in taken]
        else:
            # Walked off the cached region (cap hit, or end of timeline): restart the
            # contiguous region from what this walk saw. Older cached posts stay on disk
            # and reappear once history is extended down to them again (no re-download).
            assert last_page is not None
            acc.order = [x for x in prefix if x not in new_head]
            s.cursor = last_page.next_cursor
            s.complete = not last_page.has_next or not last_page.next_cursor
            if new_ts:
                s.oldest_ts = min(new_ts)
            log.info("@%s: refresh did not reach cached posts; history frontier reset", username)
        # formerly pinned posts go back to their place by date; stray page-1 posts likewise
        for sc in [x for x in old_head if x not in new_head] + stray:
            self._place(acc, sc)
        self.store.save_account(acc)
        self.events.posts_changed(username)
