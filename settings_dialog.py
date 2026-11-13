"""Beállítások (v0.9.0): the ⚙ gear in the main window opens this one dialog with three pages:
    Mellékek        the safer extension editor (extensions_dialog.ExtensionsEditor)
    Fejlesztői mód  the developer-mode switch (+ "Fejlesztői eszközök megnyitása" when it is on)
    Névjegy         version, author, update state, "Frissítések keresése"
Pages are picked with segmented pills (one click). Changes are saved at once by the page that makes them.
"""
from __future__ import annotations

import re
import time

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QDialog, QHBoxLayout, QLabel, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from extensions_dialog import QSS, ExtensionsEditor
from status_window import SUBTEXT

AUTHOR = "Dékány Richárd"
COPYRIGHT = "© 2026 Dékány Richárd"


def update_state_hu(text: str) -> str:
    """updater.UpdateManager.state_text (English, also used in the tray tooltip) in plain Hungarian."""
    t = text or ""
    rules = [
        (r"^Not checked yet", "Még nem volt ellenőrzés"),
        (r"^Automatic updates are off.*", "Az automatikus frissítés ki van kapcsolva"),
        (r"^Up to date \((.+)\)", r"Naprakész (\1)"),
        (r"^(\S+) available \(not an installed copy.*", r"Elérhető: \1 (forrásból futó példány, nem frissül)"),
        (r"^Downloading (.+)", r"Letöltés: \1"),
        (r"^(\S+) ready, installs when no call is active", r"\1 letöltve – telepítés, amikor nincs hívás"),
        (r"^Installing (\S+) failed.*", r"A(z) \1 telepítése nem sikerült (update-install.log)"),
        (r"^Update check failed.*", "A frissítés ellenőrzése nem sikerült (app.log)"),
        (r"^Could not start the installer.*", "A telepítő nem indult el (app.log)"),
    ]
    for pat, rep in rules:
        if re.match(pat, t):
            return re.sub(pat, rep, t)
    return t


class SettingsDialog(QDialog):
    PAGES = (("ext", "Mellékek"), ("dev", "Fejlesztői mód"), ("about", "Névjegy"))

    def __init__(self, settings, version: str, update_state=lambda: "", check_updates=None,
                 set_dev_mode=None, open_dev_tools=None, parent=None, page: str = "ext"):
        super().__init__(parent)
        self.settings = settings
        self._update_state = update_state
        self._check_updates = check_updates
        self._set_dev_mode = set_dev_mode
        self._open_dev_tools = open_dev_tools
        self.setWindowTitle("Beállítások – Call Companion")
        self.setMinimumSize(480, 500)
        self.setStyleSheet(QSS)
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        title = QLabel("Beállítások")
        title.setStyleSheet("font-size: 17px; font-weight: 700;")
        root.addWidget(title)
        nav = QHBoxLayout()
        nav.setSpacing(2)
        group = QButtonGroup(self)
        self.stack = QStackedWidget()
        self._pages = {}
        for key, label in self.PAGES:
            b = QPushButton(label, objectName="seg")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: self.show_page(k))
            group.addButton(b)
            nav.addWidget(b)
            self._pages[key] = b
        nav.addStretch(1)
        root.addLayout(nav)

        self.stack.addWidget(ExtensionsEditor(settings))
        self.stack.addWidget(self._dev_page())
        self.stack.addWidget(self._about_page(version))
        root.addWidget(self.stack, 1)

        foot = QHBoxLayout()
        foot.addStretch(1)
        close = QPushButton("Bezárás")
        close.clicked.connect(self.accept)
        foot.addWidget(close)
        root.addLayout(foot)

        self._timer = QTimer(self)             # keeps the update state fresh while the dialog is open
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._refresh_update)
        self._timer.start()
        self.show_page(page)

    def show_page(self, key: str) -> None:
        keys = [k for k, _l in self.PAGES]
        key = key if key in keys else "ext"
        self._pages[key].setChecked(True)
        self.stack.setCurrentIndex(keys.index(key))

    # ---- pages -----------------------------------------------------------------------------------------------------
    @staticmethod
    def _hint(text: str) -> QLabel:
        lab = QLabel(text)
        lab.setWordWrap(True)
        lab.setStyleSheet(f"color: {SUBTEXT}; font-size: 12px;")
        return lab

    def _dev_page(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self.dev_switch = QCheckBox("Fejlesztői mód")
        self.dev_switch.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dev_switch.setChecked(self.settings.dev_mode)
        self.dev_switch.toggled.connect(self._dev_toggled)
        lay.addWidget(self.dev_switch)
        lay.addWidget(self._hint("Bekapcsolva a tálcamenüben megjelennek a technikai eszközök: részletes állapot, "
                                 "MiniCRM és Bria ellenőrzés, hívásszimulátor, élő napló, frissítés keresése. "
                                 "A mindennapi használathoz nem szükséges."))
        self.dev_open = QPushButton("🛠 Fejlesztői eszközök megnyitása")
        self.dev_open.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dev_open.clicked.connect(self._open_dev)
        self.dev_open.setVisible(self.settings.dev_mode)
        lay.addWidget(self.dev_open, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addStretch(1)
        return w

    def _about_page(self, version: str) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(6)
        name = QLabel("Call Companion")
        name.setStyleSheet("font-size: 20px; font-weight: 700;")
        lay.addWidget(name)
        ver = QLabel(f"Verzió: {version}")
        ver.setStyleSheet(f"color: {SUBTEXT};")
        lay.addWidget(ver)
        lay.addSpacing(10)
        lay.addWidget(QLabel(f"Tervezte és készítette: {AUTHOR}"))
        cp = QLabel(COPYRIGHT)
        cp.setStyleSheet(f"color: {SUBTEXT};")
        lay.addWidget(cp)
        lay.addSpacing(14)
        self.upd_label = self._hint("")
        lay.addWidget(self.upd_label)
        self.upd_btn = QPushButton("Frissítések keresése")
        self.upd_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.upd_btn.clicked.connect(self._check)
        self.upd_btn.setVisible(self._check_updates is not None)
        lay.addWidget(self.upd_btn, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addStretch(1)
        self._refresh_update()
        return w

    # ---- actions ---------------------------------------------------------------------------------------------------
    def _dev_toggled(self, on: bool) -> None:
        self.dev_open.setVisible(on)
        if self._set_dev_mode is not None:
            self._set_dev_mode(on)

    def _open_dev(self) -> None:
        self.accept()                          # the dialog is modal: close it, then open the developer tools
        if self._open_dev_tools is not None:
            QTimer.singleShot(0, self._open_dev_tools)

    def _check(self) -> None:
        if self._check_updates is not None:
            self._check_updates()
            self._checking_until = time.monotonic() + 1.5     # the check runs in the background
            self.upd_label.setText("Frissítés keresése…")

    def _refresh_update(self) -> None:
        if getattr(self, "upd_label", None) is None or time.monotonic() < getattr(self, "_checking_until", 0):
            return
        try:
            text = update_state_hu(self._update_state())
        except Exception:
            text = ""
        self.upd_label.setText(f"Frissítés: {text}" if text else "")
