"""Location of the app's data directory (all plain-text JSON + media files)."""
from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    override = os.environ.get("IGREADER_DATA")
    if override:
        base = Path(override)
    else:
        xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        base = Path(xdg) / "igreader"
        if os.environ.get("IGREADER_FAKE"):
            base = base.with_name("igreader-fake")
    base.mkdir(parents=True, exist_ok=True)
    return base
