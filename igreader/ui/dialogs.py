"""Add-account helper and the settings dialog."""
from __future__ import annotations

import re
from typing import Optional

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QSpinBox, QVBoxLayout

from ..store import Store

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


class SettingsDialog(QDialog):
    def __init__(self, store: Store, parent=None):
        super().__init__(parent)
        self.store = store
        self.ua_changed = False
        self.setWindowTitle("Settings")
        self.setMinimumWidth(560)

        info = QLabel(
            "IGReader browses Instagram <b>logged out</b>, like a visitor of the public website — "
            "no account is used. Only public accounts and their profile-grid posts can be shown."
        )
        info.setWordWrap(True)
        self.hours = QSpinBox()
        self.hours.setRange(1, 168)
        self.hours.setSuffix(" h")
        self.hours.setValue(int(store.settings.get("refresh_hours", 6)))
        self.ua = QLineEdit(store.settings.get("user_agent") or "")
        self.ua.setPlaceholderText("leave empty for the default desktop Chrome user agent")

        form = QFormLayout()
        form.addRow("Auto-refresh on startup if last check is older than:", self.hours)
        form.addRow("User agent:", self.ua)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(info)
        lay.addLayout(form)
        lay.addWidget(btns)

    def _save(self):
        s = self.store.settings
        s["refresh_hours"] = self.hours.value()
        ua = self.ua.text().strip() or None
        self.ua_changed = ua != s.get("user_agent")
        s["user_agent"] = ua
        self.store.save_state()
        self.accept()
