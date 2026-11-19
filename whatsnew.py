"""Újdonságok (v1.1.0): after an update the app shows once what changed, in plain Hungarian (assets/whatsnew.md,
bundled by callcompanion.spec with the rest of assets/).

Which sections are shown: those with last_seen < version <= current, compared on the release part (1.1.0-beta.1
counts as 1.1.0). A fresh installation shows nothing (last_seen is set to the current version silently). A copy that
was updated from 1.0.x has no last_seen yet: main.py treats it as "1.0.0" when the user already has a history
database. The tray menu "Újdonságok" shows the newest sections at any time.

The window is not modal and never takes the place of a call card: main.py opens it a few seconds after the start,
and only when no call is ringing or running (otherwise it waits).
"""
from __future__ import annotations

import html
import logging
import re
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget

import config
import theme
from updater import version_key

log = logging.getLogger("callcompanion.whatsnew")

_HEAD = re.compile(r"^##\s+(\d+\.\d+\.\d+)\s*$")


def _base(v: str):
    """Release part of a version as a sort key: '1.1.0-beta.1' -> key of '1.1.0' (None if not a version)."""
    return version_key(str(v or "").split("-")[0].split("+")[0])


def parse(text: str) -> list[tuple[str, list[str]]]:
    """[(version, [bullet, ...]), ...] in file order (newest first by convention)."""
    out: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        m = _HEAD.match(line.strip())
        if m:
            out.append((m.group(1), []))
        elif out and line.strip().startswith("- "):
            out[-1][1].append(line.strip()[2:].strip())
        elif out and out[-1][1] and line.startswith("  ") and line.strip():
            out[-1][1][-1] += " " + line.strip()          # continuation of a wrapped bullet
    return out


def load() -> list[tuple[str, list[str]]]:
    try:
        return parse(Path(config.resource_path("assets/whatsnew.md")).read_text(encoding="utf-8"))
    except OSError:
        log.warning("whatsnew.md not found")
        return []


def pending(sections, last_seen: str | None, current: str) -> list[tuple[str, list[str]]]:
    lo, hi = _base(last_seen or "0.0.0"), _base(current)
    if lo is None or hi is None:
        return []
    return [s for s in sections if (k := _base(s[0])) is not None and lo < k <= hi]


def _md_inline(text: str) -> str:
    """**bold** -> <b>bold</b> (the only markup the notes use); everything else is escaped."""
    t = html.escape(text)
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)


class WhatsNewDialog(QDialog):
    def __init__(self, sections, version: str, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle("Újdonságok – Call Companion")
        self.setMinimumSize(480, 420)
        self.resize(540, 520)
        self.setStyleSheet(theme.qss_main() + "QScrollArea { background: transparent; border: none; }")
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(10)
        title = QLabel("Újdonságok")
        title.setStyleSheet("font-size: 19px; font-weight: 700;")
        root.addWidget(title)
        sub = QLabel(f"A Call Companion frissült: {version}")
        theme.set_tone(sub, "subtext")
        root.addWidget(sub)

        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(0, 4, 6, 4)
        bl.setSpacing(6)
        pal = theme.palette()
        for ver, bullets in sections:
            h = QLabel(f"Verzió {ver}")
            h.setStyleSheet(f"font-size: 13px; font-weight: 700; color: {pal.lavender}; padding-top: 8px;")
            bl.addWidget(h)
            for b in bullets:
                lab = QLabel(f"<table><tr><td style='padding-right:8px'>•</td><td>{_md_inline(b)}</td></tr></table>")
                lab.setTextFormat(Qt.TextFormat.RichText)
                lab.setWordWrap(True)
                bl.addWidget(lab)
        bl.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        foot = QHBoxLayout()
        foot.addStretch(1)
        ok = QPushButton("Rendben", objectName="primary")
        ok.setCursor(Qt.CursorShape.PointingHandCursor)
        ok.clicked.connect(self.accept)
        foot.addWidget(ok)
        root.addLayout(foot)
