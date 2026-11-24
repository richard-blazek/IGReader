"""Anonymous (logged-out) Instagram access — no account, nothing that can be banned.

Instaloader is used for issuing requests (it knows the current query ids, tokens and headers
of the logged-out web app); we page through with our own stored cursors and parse the raw
nodes ourselves. Every request goes through Throttle.before_api() and is logged; nothing is
ever retried automatically.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol

import requests

from .throttle import Throttle

log = logging.getLogger("igreader.ig")


# --- data passed from the client to the fetcher -------------------------------------------

@dataclass
class RawMedia:
    kind: str  # image | video
    url: str  # full-size image, or video file
    thumb_url: Optional[str] = None  # cover image for videos
    width: Optional[int] = None
    height: Optional[int] = None


@dataclass
class RawPost:
    shortcode: str
    taken_at: int
    caption: str
    out_of_order: bool  # flagged pinned -> not part of the date-ordered timeline
    is_reel: bool
    media: list[RawMedia] = field(default_factory=list)


@dataclass
class Stub:
    """A timeline node that lacked data; fetch_post(shortcode) gives the full post."""
    shortcode: str
    out_of_order: bool = False


@dataclass
class Page:
    items: list  # RawPost | Stub
    next_cursor: Optional[str]
    has_next: bool


@dataclass
class ProfileInfo:
    username: str
    user_id: str
    full_name: str
    is_private: bool
    pic_url: Optional[str]
    page_id: str = ""  # the profile page's "id" (not the user id), used for pagination


class IGError(Exception):
    """kind: block (rate limit / login wall -> cooldown), network (no cooldown),
    account (profile missing/private), unavailable (one post can't be shown logged out),
    paused (cooldown active), other."""

    def __init__(self, kind: str, message: str, code_error: bool = False):
        super().__init__(message)
        self.kind = kind
        # Our code and the installed instaloader don't fit together (AttributeError, TypeError, ...)
        # rather than Instagram answering unexpectedly. Used to roll back an instaloader update.
        self.code_error = code_error


CODE_ERRORS = (AttributeError, TypeError, NameError, ImportError)


class Client(Protocol):
    def resolve_profile(self, username: str) -> ProfileInfo: ...
    def posts_page(self, acc, cursor: Optional[str]) -> Page: ...  # acc: store.Account
    def fetch_post(self, shortcode: str) -> RawPost: ...
    def download(self, url: str, dest: Path) -> None: ...


# --- parsing of Instagram's media structs --------------------------------------------------

def _best_image(node: dict) -> Optional[dict]:
    cands = (node.get("image_versions2") or {}).get("candidates") or []
    if not cands:
        return None
    return max(cands, key=lambda c: (c.get("width") or 0) * (c.get("height") or 0))


def _media_of(node: dict) -> Optional[RawMedia]:
    img = _best_image(node)
    if node.get("media_type") == 2:
        vids = node.get("video_versions") or []
        if not vids:
            return None
        # Logged-out data has no sizes per version; the best quality comes first (max keeps the first).
        vid = max(vids, key=lambda v: (v.get("width") or 0) * (v.get("height") or 0))
        return RawMedia("video", vid["url"], img["url"] if img else None,
                        vid.get("width") or node.get("original_width"),
                        vid.get("height") or node.get("original_height"))
    if img is None:
        return None
    return RawMedia("image", img["url"], None, img.get("width") or node.get("original_width"),
                    img.get("height") or node.get("original_height"))


def parse_media_node(node: dict) -> Optional[RawPost]:
    """Parse an API v1 style media dict. Returns None if it lacks fields we need."""
    try:
        code = node["code"]
        taken_at = int(node["taken_at"])
        mtype = node["media_type"]
    except (KeyError, TypeError, ValueError):
        return None
    if "caption" not in node:
        return None
    cap = node.get("caption")
    caption = (cap.get("text") if isinstance(cap, dict) else cap) or ""
    media: list[RawMedia] = []
    if mtype == 8:
        for child in node.get("carousel_media") or []:
            m = _media_of(child)
            if m is None:
                return None
            media.append(m)
    else:
        m = _media_of(node)
        if m is None:
            return None
        media.append(m)
    if not media:
        return None
    pinned = bool(node.get("timeline_pinned_user_ids"))
    return RawPost(
        shortcode=code,
        taken_at=taken_at,
        caption=caption,
        out_of_order=pinned,
        is_reel=node.get("product_type") == "clips",
        media=media,
    )


# --- the real client -------------------------------------------------------------------------

NETWORK_MARKERS = ("HTTPSConnectionPool", "Max retries exceeded", "NameResolutionError",
                   "Failed to resolve", "Connection refused", "Read timed out", "ConnectTimeout",
                   "Network is unreachable", "RemoteDisconnected", "ConnectionResetError")


def classify(err: BaseException) -> IGError:
    import instaloader.exceptions as ex
    msg = str(err) or type(err).__name__
    if isinstance(err, IGError):
        return err
    if isinstance(err, ex.ProfileNotExistsException):
        return IGError("account", f"Profile does not exist: {msg}")
    if isinstance(err, ex.PrivateProfileNotFollowedException):
        return IGError("account", "Profile is private")
    if isinstance(err, ex.QueryReturnedNotFoundException):
        return IGError("account", f"Not found: {msg}")
    if isinstance(err, ex.TooManyRequestsException):
        return IGError("block", f"Rate limited by Instagram (429): {msg}")
    if isinstance(err, ex.AbortDownloadException):
        return IGError("block", f"Instagram refused requests: {msg}")
    if isinstance(err, ex.LoginRequiredException):
        return IGError("block", "Instagram showed a login wall to anonymous requests (IP rate limit)")
    if isinstance(err, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return IGError("network", f"Network error: {msg}")
    if isinstance(err, ex.ConnectionException):
        if any(m in msg for m in NETWORK_MARKERS):
            return IGError("network", f"Network error: {msg}")
        if "please wait" in msg.lower() or "401" in msg:
            return IGError("block", f"Instagram asked to wait: {msg}")
        # Unknown API failure: be conservative and treat as pushback.
        return IGError("block", f"Instagram request failed: {msg}")
    if isinstance(err, ex.BadResponseException):
        return IGError("block", f"Instagram returned an unexpected page (possibly rate limited): {msg}")
    if isinstance(err, ex.QueryReturnedBadRequestException):
        return IGError("block", f"Instagram rejected a request (400): {msg}")
    return IGError("other", f"{type(err).__name__}: {msg}", code_error=isinstance(err, CODE_ERRORS))


def _timeline_connection(resp: dict) -> dict:
    if resp.get("errors"):
        msg = "; ".join(str(e.get("message")) for e in resp["errors"])
        raise IGError("block", f"Instagram refused the query: {msg}")
    try:
        return resp["data"]["node"]["polaris_ordered_timeline_connection"]
    except (KeyError, TypeError) as e:
        raise IGError("block", "Unexpected answer to the timeline query (possibly rate limited)") from e


PAGINATION_DOC_ID = "28904005159232689"  # PolarisLoggedOutDesktopWWWProfilePostsTabContentQuery (instaloader 4.16)
PAGE_LENGTH = 12
FIRST_PAGE_TTL = 600


class AnonClient:
    def __init__(self, throttle: Throttle, user_agent: Optional[str] = None):
        import instaloader
        from instaloader.instaloadercontext import RateController

        self.throttle = throttle
        th = throttle

        class _RC(RateController):
            def sleep(self, secs):  # instaloader's own rate tracking -> interruptible, visible
                th.sleep(secs, "Instagram rate budget", scaled=False)

            def handle_429(self, query_type):  # never wait-and-retry: abort, cooldown handles it
                raise instaloader.exceptions.TooManyRequestsException("429 Too Many Requests")

        self.L = instaloader.Instaloader(
            quiet=True, user_agent=user_agent, max_connection_attempts=1, iphone_support=False,
            download_pictures=False, download_videos=False, download_video_thumbnails=False,
            download_geotags=False, download_comments=False, save_metadata=False,
            rate_controller=lambda ctx: _RC(ctx),
        )
        self.ctx = self.L.context  # never logged in
        self.cdn = requests.Session()
        self.cdn.headers["User-Agent"] = self.ctx.user_agent
        # username -> (fetched_at, first page connection) from the profile page
        self._first_pages: dict[str, tuple[float, dict]] = {}

    def _call(self, fn, what: str):
        self.throttle.before_api()
        log.info("IG request: %s", what)
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — everything gets classified
            raise classify(e) from e

    def _profile_page(self, username: str) -> dict:
        """One request: the public profile page, with profile data and the first posts page."""
        import instaloader.exceptions as ex

        def go():
            try:
                page = self.ctx.get_page_data(f"{username}/")
            except ex.QueryReturnedNotFoundException:
                page = []
            user: dict = {}
            for data in page:
                user.update(data.get("xig_user_by_username") or {})
            if not user.get("pk"):
                raise IGError("account", f"Profile @{username} does not exist")
            return user
        user = self._call(go, f"profile page @{username}")
        conn = user.get("polaris_ordered_timeline_connection")
        if conn is not None:
            self._first_pages[username] = (time.monotonic(), conn)
        return user

    def resolve_profile(self, username: str) -> ProfileInfo:
        user = self._profile_page(username)
        return ProfileInfo(
            username=(user.get("username") or username).lower(),
            user_id=str(user["pk"]),
            full_name=user.get("full_name") or "",
            is_private=bool(user.get("is_private")),
            pic_url=user.get("profile_pic_url_hd") or user.get("profile_pic_url"),
            page_id=str(user.get("id") or ""),
        )

    @staticmethod
    def _to_page(conn: dict) -> Page:
        items: list = []
        for edge in conn.get("edges") or []:
            node = edge.get("node") or {}
            post = parse_media_node(node)
            if post is not None:
                items.append(post)
            elif node.get("code"):
                items.append(Stub(node["code"], bool(node.get("timeline_pinned_user_ids"))))
        info = conn.get("page_info") or {}
        return Page(items, info.get("end_cursor"), bool(info.get("has_next_page")))

    def posts_page(self, acc, cursor: Optional[str]) -> Page:
        username = acc.username
        if cursor is None:
            cached = self._first_pages.pop(username, None)
            if cached is None or time.monotonic() - cached[0] > FIRST_PAGE_TTL:
                user = self._profile_page(username)
                if user.get("is_private"):
                    raise IGError("account", f"@{username} is private")
                acc.page_id = str(user.get("id") or acc.page_id)
                cached = self._first_pages.pop(username, None)
                if cached is None:
                    raise IGError("other", f"The profile page of @{username} contained no posts")
            return self._to_page(cached[1])
        if not acc.page_id:
            raise IGError("other", f"Missing page id for @{username}")
        variables = {
            "id": acc.page_id,
            "__relay_internal__pv__PolarisSEOIsCrawlerrelayprovider": False,
            # pagination, as instaloader's NodeIterator sends it
            "after": cursor, "before": None, "first": PAGE_LENGTH, "last": None,
        }
        resp = self._call(
            lambda: self.ctx.doc_id_graphql_query(PAGINATION_DOC_ID, variables,
                                                  f"https://www.instagram.com/{username}/", web_api=True),
            f"@{username} posts page cursor={cursor[:16]}…")
        return self._to_page(_timeline_connection(resp))

    def fetch_post(self, shortcode: str) -> RawPost:
        """Full post data from its public page (instagram.com/p/<code>/), which embeds it.
        Instagram rate-limits the logged-out GraphQL post query almost immediately; the
        page load is what the logged-out website itself does."""
        def go():
            for data in self.ctx.get_page_data(f"p/{shortcode}/"):
                xm = data.get("xig_polaris_media")
                if xm is not None:
                    return xm
            raise IGError("block", "Instagram served a post page without the post (anonymous rate limit)")
        xm = self._call(go, f"post page {shortcode}")
        node = xm.get("if_not_gated_logged_out")
        if not node:
            raise IGError("unavailable", f"Post {shortcode} is not viewable logged out (age/region gated)")
        post = parse_media_node(node)
        if post is None:
            raise IGError("unavailable", f"Post {shortcode} has no displayable media")
        return post

    def download(self, url: str, dest: Path) -> None:
        self.throttle.before_media()
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        try:
            with self.cdn.get(url, stream=True, timeout=60) as r:
                if r.status_code == 429:
                    raise IGError("block", "CDN rate limited (429)")
                if r.status_code != 200:
                    raise IGError("other", f"Media download failed: HTTP {r.status_code}")
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(256 * 1024):
                        self.throttle.check()
                        f.write(chunk)
            os.replace(tmp, dest)
        except requests.RequestException as e:
            raise IGError("network", f"Network error downloading media: {e}") from e
        finally:
            tmp.unlink(missing_ok=True)


def self_check() -> None:
    """Offline check that the installed instaloader still offers everything AnonClient uses.
    Raises on any incompatibility (run after an instaloader update, see updater.py)."""
    import inspect

    import instaloader
    import instaloader.exceptions as ex
    from instaloader.instaloadercontext import InstaloaderContext, RateController

    def params(fn) -> set[str]:
        return set(inspect.signature(fn).parameters)

    need_init = {"quiet", "user_agent", "max_connection_attempts", "iphone_support", "download_pictures",
                 "download_videos", "download_video_thumbnails", "download_geotags", "download_comments",
                 "save_metadata", "rate_controller"}
    missing = need_init - params(instaloader.Instaloader.__init__)
    assert not missing, f"Instaloader() lacks {missing}"
    assert {"doc_id", "variables", "referer", "web_api"} <= params(InstaloaderContext.doc_id_graphql_query)
    assert {"path"} <= params(InstaloaderContext.get_page_data)
    assert {"secs"} <= params(RateController.sleep)
    assert {"query_type"} <= params(RateController.handle_429)
    for name in ("ProfileNotExistsException", "PrivateProfileNotFollowedException",
                 "QueryReturnedNotFoundException", "TooManyRequestsException", "AbortDownloadException",
                 "LoginRequiredException", "ConnectionException", "BadResponseException",
                 "QueryReturnedBadRequestException"):
        assert isinstance(getattr(ex, name), type), name
    c = AnonClient(Throttle(scale=0))  # no network access on construction
    assert not c.ctx.is_logged_in and c.ctx.user_agent
