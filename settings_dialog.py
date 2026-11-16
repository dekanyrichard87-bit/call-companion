"""Beállítások (v0.9.0): the ⚙ gear in the main window opens this one dialog with four pages:
    Mellékek        the safer extension editor (extensions_dialog.ExtensionsEditor)
    Megjelenés      v0.10.0: Sötét / Világos / Rendszer szerint (three painted tiles, live switch)
    Fejlesztői mód  the developer-mode switch (+ "Fejlesztői eszközök megnyitása" when it is on)
    Névjegy         version, author, update state, "Frissítések keresése"
Pages are picked with segmented pills (one click). Changes are saved at once by the page that makes them.
"""
from __future__ import annotations

import re
import time

from PySide6.QtCore import QPointF, QRectF, QSize, QTimer, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractButton, QButtonGroup, QCheckBox, QDialog, QHBoxLayout, QLabel, QPushButton, QStackedWidget,
    QVBoxLayout, QWidget,
)

import theme
from extensions_dialog import ExtensionsEditor

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


class ThemeTile(QAbstractButton):
    """v0.10.0 Megjelenés tile (design canvas "Beállítások – 0.10"): a 64 px mini preview of the theme and the name
    with a drawn icon (moon / sun / monitor). The frame follows the current theme; the preview always shows its own
    theme. Checked = blue 2 px border."""
    # preview colours: (background, text bars, green pill)
    PREVIEW = {"dark": ("#1e1e2e", "#cdd6f4", "#a6e3a1"), "light": ("#eff1f5", "#4c4f69", "#2e7d1f")}

    def __init__(self, key: str, label: str, parent=None):
        super().__init__(parent)
        self.key = key
        self.setText(label)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumSize(120, 114)
        self.setAccessibleName(label)

    def sizeHint(self):
        return QSize(150, 114)

    def enterEvent(self, e):
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _):
        pal = theme.palette()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        edge = pal.blue if self.isChecked() else (pal.overlay if self.underMouse() else pal.surface2)
        p.setPen(QPen(QColor(edge), 2))
        p.setBrush(QColor(pal.surface))
        p.drawRoundedRect(r, 12, 12)
        if self.hasFocus():
            p.setPen(QPen(QColor(pal.blue), 1, Qt.PenStyle.DotLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r.adjusted(3, 3, -3, -3), 9, 9)

        pv = QRectF(r.left() + 10, r.top() + 10, r.width() - 20, 64)
        clip = QPainterPath()
        clip.addRoundedRect(pv, 8, 8)
        p.save()
        p.setClipPath(clip)
        p.setPen(Qt.PenStyle.NoPen)
        if self.key == "system":
            half = pv.width() / 2
            p.fillRect(QRectF(pv.left(), pv.top(), half, pv.height()), QColor(self.PREVIEW["dark"][0]))
            p.fillRect(QRectF(pv.left() + half, pv.top(), pv.width() - half, pv.height()),
                       QColor(self.PREVIEW["light"][0]))
        else:
            bg, fg, pill = self.PREVIEW[self.key]
            p.fillRect(pv, QColor(bg))
            inner = pv.adjusted(8, 8, -8, -8)
            p.setBrush(QColor(fg))
            p.drawRoundedRect(QRectF(inner.left(), inner.top(), inner.width() * 0.6, 8), 4, 4)
            dim = QColor(fg)
            dim.setAlphaF(0.5)
            p.setBrush(dim)
            p.drawRoundedRect(QRectF(inner.left(), inner.top() + 12, inner.width() * 0.8, 6), 3, 3)
            p.setBrush(QColor(pill))
            p.drawRoundedRect(QRectF(inner.left(), inner.bottom() - 14, inner.width() * 0.45, 14), 4, 4)
        p.restore()

        y = pv.bottom() + 8
        icon = QRectF(r.left() + 10, y + 2.5, 15, 15)
        self._paint_icon(p, icon, QColor(pal.text))
        f = QFont("Segoe UI")
        f.setPixelSize(13)
        f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        p.setPen(QColor(pal.text))
        p.drawText(QRectF(icon.right() + 6, y, r.right() - icon.right() - 12, 20),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text())
        p.end()

    def _paint_icon(self, p: QPainter, box: QRectF, col: QColor):
        """The canvas' 24-unit icons (stroke 1.8), scaled into `box`."""
        p.save()
        p.translate(box.topLeft())
        k = box.width() / 24.0
        p.scale(k, k)
        p.setPen(QPen(col, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        if self.key == "dark":                      # crescent moon
            outer, cut = QPainterPath(), QPainterPath()
            outer.addEllipse(QPointF(12, 12.5), 8, 8)
            cut.addEllipse(QPointF(16.5, 8), 6.5, 6.5)
            p.drawPath(outer.subtracted(cut))
        elif self.key == "light":                   # sun
            p.drawEllipse(QPointF(12, 12), 4, 4)
            for a, b in (((12, 2), (12, 4)), ((12, 20), (12, 22)), ((4.9, 4.9), (6.3, 6.3)),
                         ((17.7, 17.7), (19.1, 19.1)), ((2, 12), (4, 12)), ((20, 12), (22, 12)),
                         ((4.9, 19.1), (6.3, 17.7)), ((17.7, 6.3), (19.1, 4.9))):
                p.drawLine(QPointF(*a), QPointF(*b))
        else:                                       # monitor
            p.drawRoundedRect(QRectF(3, 4, 18, 12), 2, 2)
            p.drawLine(QPointF(8, 20), QPointF(16, 20))
            p.drawLine(QPointF(12, 16), QPointF(12, 20))
        p.restore()


class SettingsDialog(QDialog):
    PAGES = (("ext", "Mellékek"), ("look", "Megjelenés"), ("dev", "Fejlesztői mód"), ("about", "Névjegy"))
    THEMES = (("dark", "Sötét"), ("light", "Világos"), ("system", "Rendszer szerint"))

    def __init__(self, settings, version: str, update_state=lambda: "", check_updates=None,
                 set_dev_mode=None, open_dev_tools=None, parent=None, page: str = "ext"):
        super().__init__(parent)
        self.settings = settings
        self._update_state = update_state
        self._check_updates = check_updates
        self._set_dev_mode = set_dev_mode
        self._open_dev_tools = open_dev_tools
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)   # v0.10.0: gone after closing (no hidden copies
        self.setWindowTitle("Beállítások – Call Companion")       # left behind that would restyle on a switch)
        self.setMinimumSize(480, 500)
        self.setStyleSheet(theme.qss_main())
        theme.changed.connect(self._theme_changed)
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
        self.stack.addWidget(self._look_page())
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
        lab = QLabel(text, objectName="hint")          # subtext, 12 px (theme.qss_main)
        lab.setWordWrap(True)
        return lab

    def _look_page(self) -> QWidget:
        """v0.10.0 Megjelenés: Sötét / Világos / Rendszer szerint. One click switches at once (saved too)."""
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        head = QLabel("Téma")
        head.setStyleSheet("font-size: 13px; font-weight: 600;")
        lay.addWidget(head)
        row = QHBoxLayout()
        row.setSpacing(10)
        group = QButtonGroup(self)
        group.setExclusive(True)
        self._tiles = {}
        current = self.settings.theme
        for key, label in self.THEMES:
            t = ThemeTile(key, label)
            t.setChecked(key == current)
            t.clicked.connect(lambda _=False, k=key: self._pick_theme(k))
            group.addButton(t)
            row.addWidget(t, 1)
            self._tiles[key] = t
        lay.addLayout(row)
        lay.addWidget(self._hint("A „Rendszer szerint” a Windows alkalmazás-témáját követi (Beállítások › "
                                 "Személyre szabás › Színek). A váltás azonnal érvényes, újraindítás nélkül – a "
                                 "hívókártyára is."))
        lay.addStretch(1)
        return w

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
        theme.set_tone(ver, "subtext")
        lay.addWidget(ver)
        lay.addSpacing(10)
        lay.addWidget(QLabel(f"Tervezte és készítette: {AUTHOR}"))
        cp = QLabel(COPYRIGHT)
        theme.set_tone(cp, "subtext")
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
    def _pick_theme(self, key: str) -> None:
        self.settings.set_theme(key)
        theme.set_mode(key)                    # live: every open window and card re-styles at once

    def _theme_changed(self) -> None:
        self.setStyleSheet(theme.qss_main())
        for t in getattr(self, "_tiles", {}).values():
            t.update()

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
