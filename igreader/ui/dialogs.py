"""Add-account helper."""
from __future__ import annotations

import re
from typing import Optional

HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")


def parse_handle(text: str) -> Optional[str]:
    text = text.strip()
    m = re.search(r"instagram\.com/([A-Za-z0-9._]{1,30})", text)
    if m:
        text = m.group(1)
    text = text.lstrip("@").strip("/").lower()
    if text in ("p", "reel", "reels", "explore", "stories", "accounts"):
        return None
    return text if HANDLE_RE.match(text) else None
