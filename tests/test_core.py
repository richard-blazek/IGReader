import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from igreader.fake_client import FakeClient  # noqa: E402
from igreader.fetcher import Fetcher  # noqa: E402
from igreader.ig_client import IGError, classify, parse_media_node  # noqa: E402
from igreader.store import Account, MediaItem, Post, Store  # noqa: E402
from igreader.throttle import Throttle  # noqa: E402


class Events:
    def __init__(self):
        self.changes = 0

    def posts_changed(self, username):
        self.changes += 1

    def accounts_changed(self):
        pass


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path)
    client = FakeClient(Throttle(scale=0), tmp_path)
    client._fake_video = lambda: None  # no ffmpeg in tests
    f = Fetcher(store, client, Events())
    return store, client, f


def profile_order(client, username):
    """Ground truth: the profile grid in Instagram's order, minus posts not viewable logged out."""
    return [p.shortcode for p in client._grid(username) if int(p.shortcode.rsplit(".", 1)[1]) % 23]


def all_posts_of(client, username):
    """Ground truth: every profile-grid post of a fake account that is viewable logged out."""
    return {p.shortcode: p for p in client._timeline(username) if int(p.shortcode.rsplit(".", 1)[1]) % 23}


# --- store ---------------------------------------------------------------------------------

def test_store_roundtrip_and_remove(tmp_path):
    s = Store(tmp_path)
    s.add_account(Account("alice", user_id="1", full_name="Alice", added_at=100))
    post = Post("abc", "alice", 1_700_000_000, "hi ✨", False, 5,
                [MediaItem(0, "image", "media/abc_0.jpg", None, 1080, 1080)])
    assert s.save_post(post)
    s2 = Store(tmp_path)
    assert s2.get_post("abc") == post
    assert s2.account("alice").full_name == "Alice"
    raw = (tmp_path / "accounts/alice/posts/abc.json").read_text(encoding="utf-8")
    assert '"taken_at": "2023-11-14T22:13:20Z"' in raw and "✨" in raw  # human readable
    s2.remove_account("alice")
    assert not (tmp_path / "accounts/alice").exists()
    assert Store(tmp_path).accounts() == []
    assert not s2.save_post(post)  # refused for removed account


def test_cooldown_escalates_and_resets(tmp_path):
    s = Store(tmp_path)
    t0 = time.time()
    u1 = s.start_cooldown("x")
    assert 3500 < u1 - t0 < 3700
    u2 = s.start_cooldown("x")
    assert 6 * 3600 - 100 < u2 - t0 < 6 * 3600 + 100
    u3 = s.start_cooldown("x")
    u4 = s.start_cooldown("x")
    assert u3 - t0 > 23 * 3600 and u4 - t0 < 25 * 3600
    s.clear_cooldown_level()
    assert s.cooldown_until() is None
    assert s.start_cooldown("x") - t0 < 3700


# --- fetching ------------------------------------------------------------------------------

def test_add_account_fetches_first_pages_with_media(env):
    store, client, f = env
    f.add_account("demo60")
    acc = store.account("demo60")
    assert all(s.started for s in acc.streams.values())
    posts = store.account_posts("demo60")
    assert len(posts) == 12
    assert client.api_calls == 1 + 12  # profile page (reused as first page) + one post page per post
    for p in posts:
        for m in p.media:
            assert (store.account_dir("demo60") / m.file).exists()
    assert store.profile_pic("demo60").exists()
    # shown in the profile's own order: the two pinned posts first
    feed, complete = store.feed("demo60")
    assert not complete
    assert [p.shortcode for p in feed[:2]] == ["demo60.0009", "demo60.0005"]
    assert store.account("demo60").head == ["demo60.0009", "demo60.0005"]


def test_full_history_is_complete_correct_and_never_refetched(env):
    store, client, f = env
    f.add_account("demo60")
    while f.extend_history("demo60"):
        pass
    truth = all_posts_of(client, "demo60")
    got = {p.shortcode for p in store.account_posts("demo60")}
    assert got == set(truth)
    feed, complete = store.feed("demo60")
    assert complete and len(feed) == len(truth)
    assert [p.shortcode for p in feed] == profile_order(client, "demo60")
    # nothing left: no further requests
    calls = client.api_calls
    assert f.extend_history("demo60") is False
    assert client.api_calls == calls


def test_feed_never_shows_gaps(env):
    """At every step, every post newer than the oldest visible one is visible."""
    store, client, f = env
    for u in ("demo30", "demo45", "other70"):
        f.add_account(u)
    truth = {}
    for u in ("demo30", "demo45", "other70"):
        truth.update(all_posts_of(client, u))
    for _ in range(40):
        feed, complete = store.feed(None)
        if feed:
            oldest = feed[-1].taken_at
            expected = {sc for sc, p in truth.items() if p.taken_at >= oldest}
            assert {p.shortcode for p in feed} >= expected
            ts = [p.taken_at for p in feed]
            assert ts == sorted(ts, reverse=True)
        if complete:
            break
        u = store.limiting_account()
        assert f.extend_history(u)
    assert store.feed(None)[1]
    assert len(store.feed(None)[0]) == len(truth)


def test_refresh_fetches_only_new_posts(env):
    store, client, f = env
    f.add_account("demo40")
    before = len(store.account_posts("demo40"))
    calls, downloads = client.api_calls, client.downloads
    assert f.refresh("demo40") == 0
    assert client.api_calls - calls == 1  # just the profile page; all posts on it already known
    assert client.downloads == downloads  # nothing downloaded again
    client.extra["demo40"] = 15  # 15 new posts on top -> spans two pages
    n = f.refresh("demo40")
    expected = len([k for k in range(41, 56) if k % 23])  # #46 is gated
    assert n == expected == 14
    assert len(store.account_posts("demo40")) == before + expected
    feed, _ = store.feed("demo40")
    newest = max(all_posts_of(client, "demo40").values(), key=lambda p: p.taken_at)
    assert feed[2].shortcode == newest.shortcode  # right after the 2 pinned posts


def test_interrupted_refresh_leaves_no_gap(env):
    store, client, f = env
    f.add_account("demo40")
    client.extra["demo40"] = 8
    client.fail_download_at = client.downloads + 6
    with pytest.raises(IGError):
        f.refresh("demo40")
    client.fail_download_at = 0
    f.refresh("demo40")
    truth = all_posts_of(client, "demo40")
    feed, _ = store.feed("demo40")
    head = set(store.account("demo40").head)
    oldest = min(p.taken_at for p in feed if p.shortcode not in head)
    expected = {sc for sc, p in truth.items() if p.taken_at >= oldest}
    assert {p.shortcode for p in feed} >= expected


def test_unpinned_post_returns_to_its_place(env):
    store, client, f = env
    f.add_account("demo40")
    client.pinned_k = (5,)  # #9 unpinned on Instagram
    f.refresh("demo40")
    feed = [p.shortcode for p in store.feed("demo40")[0]]
    assert feed[0] == "demo40.0005" and "demo40.0009" not in feed  # older than the cached range
    while f.extend_history("demo40"):
        pass
    assert [p.shortcode for p in store.feed("demo40")[0]] == profile_order(client, "demo40")


def test_profile_order_kept_with_out_of_order_post(env):
    store, client, f = env
    f.add_account("odd40")
    while f.extend_history("odd40"):
        pass
    assert [p.shortcode for p in store.feed("odd40")[0]] == profile_order(client, "odd40")
    client.extra["odd40"] = 5
    f.refresh("odd40")
    assert [p.shortcode for p in store.feed("odd40")[0]] == profile_order(client, "odd40")
    merged = [p.taken_at for p in store.feed(None)[0]]
    assert merged == sorted(merged, reverse=True)  # all-accounts view stays chronological


def test_private_and_missing_accounts(env):
    store, client, f = env
    with pytest.raises(IGError) as e:
        f.add_account("private_bob")
    assert e.value.kind == "account" and store.accounts() == []
    with pytest.raises(IGError) as e:
        f.add_account("missing_x")
    assert e.value.kind == "account"


# --- parsing / classification ----------------------------------------------------------------

def test_parse_carousel_video_pinned():
    node = {
        "code": "C1", "taken_at": 1700000000, "media_type": 8, "caption": {"text": "hello"},
        "timeline_pinned_user_ids": [123], "product_type": "carousel_container",
        "carousel_media": [
            {"media_type": 1, "image_versions2": {"candidates": [
                {"url": "small", "width": 320, "height": 320}, {"url": "big", "width": 1080, "height": 1080}]}},
            {"media_type": 2, "image_versions2": {"candidates": [{"url": "cover", "width": 1080, "height": 1920}]},
             "video_versions": [{"url": "vid", "width": 720, "height": 1280}]},
        ],
    }
    p = parse_media_node(node)
    assert p.out_of_order and p.caption == "hello"
    assert [(m.kind, m.url, m.thumb_url) for m in p.media] == [("image", "big", None), ("video", "vid", "cover")]
    assert parse_media_node({"code": "x", "media_type": 2}) is None  # incomplete -> needs fetch_post
    assert parse_media_node({**node, "caption": None}).caption == ""


def test_classify():
    import instaloader.exceptions as ex
    assert classify(ex.TooManyRequestsException("429")).kind == "block"
    assert classify(ex.ConnectionException("401 Unauthorized - \"fail\" status, message \"Please wait a few minutes "
                                           "before you try again.\"")).kind == "block"
    assert classify(ex.AbortDownloadException("checkpoint_required")).kind == "block"
    assert classify(ex.LoginRequiredException("x")).kind == "block"  # login wall = anonymous rate limit
    assert classify(ex.ConnectionException("HTTPSConnectionPool(host=...): Max retries exceeded")).kind == "network"
    assert classify(ex.ProfileNotExistsException("x")).kind == "account"


def test_parse_handle():
    from igreader.ui.dialogs import parse_handle
    assert parse_handle("@NatGeo") == "natgeo"
    assert parse_handle("https://www.instagram.com/nat.geo_1/?hl=en") == "nat.geo_1"
    assert parse_handle("https://instagram.com/p/ABC/") is None
    assert parse_handle("bad handle!") is None


def test_refresh_with_few_new_posts_below_pinned(env):
    """Pinned (old, known) posts at the top of page 1 must not stop the refresh."""
    store, client, f = env
    f.add_account("demo40")
    client.extra["demo40"] = 4
    calls = client.api_calls
    assert f.refresh("demo40") == 4
    assert client.api_calls - calls == 1 + 4
    assert store.account("demo40").head == ["demo40.0009", "demo40.0005"]
    assert [p.shortcode for p in store.feed("demo40")[0]][:3] == ["demo40.0009", "demo40.0005", "demo40.0044"]
    assert f.refresh("demo40") == 0


def test_anon_client_reuses_profile_page_and_paginates(monkeypatch):
    from igreader.ig_client import AnonClient, PAGINATION_DOC_ID
    c = AnonClient(Throttle(scale=0))
    assert not c.ctx.is_logged_in
    node = {"code": "AAA", "taken_at": 1700000000, "media_type": 1, "caption": None,
            "image_versions2": {"candidates": [{"url": "u", "width": 1, "height": 1}]}}
    conn = {"edges": [{"node": node}, {"node": {"code": "BBB"}}],
            "page_info": {"has_next_page": True, "end_cursor": "CUR1"}}
    pages = []

    def page_data(path):
        pages.append(path)
        return [{"xig_user_by_username": {"pk": "42", "id": "PAGEID", "username": "someone", "full_name": "S",
                                          "is_private": False, "polaris_ordered_timeline_connection": conn}}]
    monkeypatch.setattr(c.ctx, "get_page_data", page_data)
    info = c.resolve_profile("someone")
    assert (info.user_id, info.page_id) == ("42", "PAGEID")
    acc = Account("someone", user_id=info.user_id, page_id=info.page_id)
    first = c.posts_page(acc, None)
    assert pages == ["someone/"]  # first page reused from the profile page request
    assert [type(i).__name__ for i in first.items] == ["RawPost", "Stub"] and first.next_cursor == "CUR1"

    sent = {}

    def gql(doc_id, variables, referer=None, web_api=False):
        sent.update(doc_id=doc_id, variables=variables, web_api=web_api)
        return {"data": {"node": {"polaris_ordered_timeline_connection": {
            "edges": [], "page_info": {"has_next_page": False, "end_cursor": None}}}}}
    monkeypatch.setattr(c.ctx, "doc_id_graphql_query", gql)
    second = c.posts_page(acc, "CUR1")
    assert sent["doc_id"] == PAGINATION_DOC_ID and sent["web_api"]
    assert sent["variables"]["id"] == "PAGEID" and sent["variables"]["after"] == "CUR1"
    assert not second.has_next


def test_gated_posts_are_skipped_once(env):
    store, client, f = env
    f.add_account("demo50")
    while f.extend_history("demo50"):
        pass
    assert store.account("demo50").skipped == ["demo50.0046", "demo50.0023"]
    assert not store.has_post("demo50.0023")
    calls = client.api_calls
    client.extra["demo50"] = 0
    f.refresh("demo50")
    assert client.api_calls - calls == 1  # gated posts are not requested again


def test_graphql_rate_limit_is_a_block():
    from igreader.ig_client import _timeline_connection
    with pytest.raises(IGError) as e:
        _timeline_connection({"errors": [{"message": "Rate limit exceeded"}]})
    assert e.value.kind == "block"
    import instaloader.exceptions as ex
    assert classify(ex.BadResponseException("x")).kind == "block"


# --- background checks ---------------------------------------------------------------------

class FakeWorker:
    def __init__(self):
        self.pending = set()
        self.calls = []

    def refresh(self, usernames):
        self.calls.append(list(usernames))
        return len(usernames)


def _local(y, mo, d, h, mi=0):
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


@pytest.fixture
def bg(tmp_path):
    from PySide6.QtCore import QCoreApplication
    from igreader import background
    QCoreApplication.instance() or QCoreApplication([])
    store = Store(tmp_path)
    clock = {"t": _local(2026, 6, 10, 12)}
    net = {"online": True}
    worker = FakeWorker()
    sched = background.BackgroundScheduler(store, worker, now=lambda: clock["t"], online=lambda: net["online"])
    return store, worker, sched, clock, net


def test_background_check_respects_schedule(bg):
    from igreader import background
    store, worker, sched, clock, net = bg
    noon = clock["t"]
    store.add_account(Account("fresh", last_refresh_at=int(noon - 3600)))
    store.add_account(Account("stale", last_refresh_at=int(noon - 5 * 3600)))
    store.add_account(Account("never"))
    assert sched.tick() is None  # first gap after start not over yet
    sched.next_at = noon
    net["online"] = False
    assert sched.tick() is None
    net["online"] = True
    store.start_cooldown("x")
    assert sched.tick() is None
    store.clear_cooldown_level()
    worker.pending.add(("older", None))
    assert sched.tick() is None
    worker.pending.clear()
    assert sched.tick() == 2
    assert worker.calls == [["stale", "never"]]
    lo, hi = background.GAP
    assert noon + lo <= sched.next_at <= noon + hi
    assert sched.tick() is None  # not again right away


def test_background_check_only_in_daytime(bg):
    store, worker, sched, clock, net = bg
    store.add_account(Account("a"))
    sched.next_at = 0
    for h in (23, 2, 6):
        clock["t"] = _local(2026, 6, 10, h, 30)
        assert sched.tick() is None
    clock["t"] = _local(2026, 6, 10, 8, 50)  # after the max. morning jitter
    assert sched.tick() == 1


def test_autostart_entry(tmp_path, monkeypatch):
    from igreader import background
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    background.install_autostart()
    text = background.autostart_file().read_text()
    assert "--background" in text and "Exec=" in text
    background.install_autostart()  # idempotent
    assert background.autostart_file().read_text() == text


# --- instaloader updates -------------------------------------------------------------------

def test_self_check_passes_with_installed_instaloader():
    from igreader.ig_client import self_check
    self_check()


class FakeSignal:
    def __init__(self):
        self.slots = []

    def connect(self, fn):
        self.slots.append(fn)

    def emit(self, *a):
        for fn in self.slots:
            fn(*a)


class UpdWorker:
    def __init__(self):
        self.pending = set()
        self.codeError = FakeSignal()
        self.jobSucceeded = FakeSignal()


class FakePip:
    """Simulates `python -c version`, `pip install` and the self-check subprocesses."""

    def __init__(self, installed, latest, check_ok=True):
        self.installed, self.latest, self.check_ok = installed, latest, check_ok
        self.cmds = []

    def __call__(self, cmd):
        self.cmds.append(cmd)
        if "pip" in cmd:
            req = cmd[-1]
            if "==" in req:
                self.installed = req.split("==")[1]
            elif f"!={self.latest}" not in req:
                self.installed = self.latest
            return 0, ""
        if "self_check" in cmd[-1]:
            return (0, "") if self.check_ok else (1, "AssertionError")
        return 0, self.installed


@pytest.fixture
def upd(tmp_path, monkeypatch):
    from PySide6.QtCore import QCoreApplication
    from igreader import updater
    QCoreApplication.instance() or QCoreApplication([])
    monkeypatch.setattr(updater, "can_self_update", lambda: None)

    class SyncThread:  # run the update synchronously
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            self.target()
    monkeypatch.setattr(updater.threading, "Thread", SyncThread)

    def make(pip, now=1_000_000.0):
        store = Store(tmp_path)
        clock = {"t": now}
        restarts = []
        u = updater.Updater(store, UpdWorker(), can_restart=lambda: True, restart=lambda: restarts.append(1),
                            now=lambda: clock["t"], online=lambda: True, runner=pip)
        return store, u, clock, restarts
    return make


def _tick_after(u, clock, secs):
    from PySide6.QtCore import QCoreApplication
    clock["t"] += secs
    u.tick()
    QCoreApplication.processEvents()


def test_update_installs_verifies_and_restarts(upd):
    from igreader import updater
    pip = FakePip("4.16", "4.17")
    store, u, clock, restarts = upd(pip)
    u.start()
    _tick_after(u, clock, 60)
    assert not any("pip" in c for c in pip.cmds)  # not right after start
    _tick_after(u, clock, updater.FIRST_CHECK_AFTER)
    assert pip.installed == "4.17"
    assert store.state["updates"]["trial"] == {"old": "4.16", "new": "4.17"}
    assert restarts == [1]
    u.worker.jobSucceeded.emit()
    assert "trial" not in store.state["updates"]
    n = len(pip.cmds)
    _tick_after(u, clock, 3600)
    assert len(pip.cmds) == n  # once a day only


def test_update_failing_self_check_is_rolled_back_and_skipped(upd):
    from igreader import updater
    pip = FakePip("4.16", "4.17", check_ok=False)
    store, u, clock, restarts = upd(pip)
    u.start()
    _tick_after(u, clock, updater.FIRST_CHECK_AFTER + 1)
    assert pip.installed == "4.16" and store.state["updates"]["bad"] == ["4.17"]
    assert restarts == [] and "trial" not in store.state["updates"]
    assert u.spec().endswith(",!=4.17")


def test_update_rolled_back_on_code_error_during_trial(upd):
    pip = FakePip("4.17", "4.17")
    store, u, clock, restarts = upd(pip)
    store.state["updates"] = {"trial": {"old": "4.16", "new": "4.17"}}
    u.start()
    u.worker.codeError.emit("AttributeError: x")
    from PySide6.QtCore import QCoreApplication
    QCoreApplication.processEvents()
    assert pip.installed == "4.16" and store.state["updates"]["bad"] == ["4.17"]
    assert restarts == [1]


def test_code_errors_are_flagged():
    from igreader.ig_client import classify
    assert classify(AttributeError("x")).code_error
    assert not classify(IGError("block", "429")).code_error
