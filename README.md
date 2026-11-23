# IGReader

Desktop viewer (PySide6) for posts of a hand-picked set of public Instagram accounts:
a merged chronological feed, per-account feeds back to the very first post, and all
photos/videos of each post. Everything fetched is kept locally as plain JSON + media
files and is never requested from Instagram again.

## Run

    .venv/bin/igreader                 # real mode
    IGREADER_FAKE=1 .venv/bin/igreader # offline demo data (separate data dir), no network

Install the launcher: `cp igreader.desktop ~/.local/share/applications/`

Add accounts with **+ Add** (handle or profile URL). No Instagram account is used: the app
browses **logged out**, like a visitor of the public website, so there is nothing that can be
banned. Only public accounts and their profile-grid posts are shown (the Reels tab needs a login).

## Cost and how it limits the risk of blocks

* Checking an account for new posts costs **1 request** (its public profile page); each new
  post costs one more (logged-out timelines only list post codes, so each post's metadata is
  fetched once). Older history: 1 request per page + 1 per post.
* Already-cached posts are never requested or downloaded again.
* One request at a time, randomized 5–12 s apart, 20–60 s between accounts during a refresh,
  plus Instaloader's own rate budget. No automatic retries.
* Startup refresh only for accounts not checked in the last 6 hours. Manual check:
  right-click an account → *Force refresh*.
* Older history is fetched lazily, one page (~12 posts) when you scroll past the cache.
* Any 429 / "please wait" / login wall stops all fetching and starts a cooldown
  (1 h → 6 h → 24 h on repeated pushback; reset after a clean run). Pushback affects only
  logged-out access from your IP, temporarily.
* Every request is logged to `igreader.log` in the data dir.

## Background checking

Closing the window only hides it: the process stays running and keeps the cache fresh, so
reopening is instant. Launching the app again brings back the running window; it never starts
a second copy. **Force quit** (or Ctrl+Q) ends it completely.
The app also starts hidden at login (`~/.config/autostart/igreader.desktop`).

The background checks are kept modest:

* only between ~08:00–08:45 (randomized each day) and 22:00 local time;
* only when NetworkManager reports you online and the connection isn't metered;
* about every 2.5–4 h (randomized), and only for accounts not checked in the last 3 h;
* never during a cooldown or while other fetching is running, and never older history;
* same pacing as any other refresh (one request at a time, 5–12 s apart, 20–60 s between accounts).

## Data (`~/.local/share/igreader/`)

    state.json, accounts.json, igreader.log
    accounts/<user>/account.json          pagination cursors, history frontier
    accounts/<user>/posts/<shortcode>.json
    accounts/<user>/media/<shortcode>_<i>.jpg|.mp4, profile.jpg

Removing an account deletes its folder.

## Tests

    QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests
