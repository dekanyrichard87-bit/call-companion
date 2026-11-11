"""Extension list editor (v0.8.0): the colleagues a call can be transferred to from the card.

Saved by settings.Settings in %LOCALAPPDATA%\\CallCompanion\\settings.json, so each company / PC keeps its own list.
No double-clicks: a cell is edited with one click (or by typing), rows are added, removed and moved with buttons.
"""
from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout,
)

from settings import MAX_EXTENSIONS, clean_extension
from status_window import BASE, BLUE, MANTLE, QSS as MAIN_QSS, RED, SUBTEXT, SURFACE, SURFACE2

QSS = MAIN_QSS + f"""
QTableWidget {{ background: {SURFACE}; border: none; border-radius: 10px; gridline-color: {SURFACE2};
               selection-background-color: {SURFACE2}; selection-color: #cdd6f4; }}
QTableWidget QLineEdit {{ background: {MANTLE}; border: 1px solid {BLUE}; border-radius: 4px; padding: 2px 6px; }}
QHeaderView::section {{ background: {SURFACE}; color: {SUBTEXT}; border: none; padding: 6px; font-size: 11px; }}
QPushButton#primary {{ background: {BLUE}; color: {BASE}; font-weight: 600; }}
QPushButton#primary:hover {{ background: #a6c8ff; }}
"""


class ExtensionsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Mellékek – Call Companion")
        self.setMinimumSize(460, 440)
        self.setStyleSheet(QSS)
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        title = QLabel("Belső mellékek")
        title.setStyleSheet("font-size: 17px; font-weight: 700;")
        root.addWidget(title)
        intro = QLabel("Ezekre a mellékekre lehet egy kattintással átkapcsolni a hívást a hívás kártyáján "
                       "(↪ Átkapcsolás). Kattintson egy cellába a szerkesztéshez.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {SUBTEXT}; font-size: 12px;")
        root.addWidget(intro)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Mellék", "Kolléga neve"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.CurrentChanged
                                   | QAbstractItemView.EditTrigger.SelectedClicked
                                   | QAbstractItemView.EditTrigger.AnyKeyPressed
                                   | QAbstractItemView.EditTrigger.EditKeyPressed)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 110)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.table, 1)
        for number, name in settings.extensions:
            self._add_row(number, name)

        tools = QHBoxLayout()
        add = QPushButton("+ Új mellék")
        add.clicked.connect(self._new_row)
        rm = QPushButton("Törlés")
        rm.clicked.connect(self._remove)
        up = QPushButton("▲")
        up.setToolTip("Feljebb (a kártyán is ebben a sorrendben jelennek meg)")
        up.clicked.connect(lambda: self._move(-1))
        down = QPushButton("▼")
        down.setToolTip("Lejjebb")
        down.clicked.connect(lambda: self._move(1))
        for b in (add, rm, up, down):
            tools.addWidget(b)
        tools.addStretch(1)
        root.addLayout(tools)

        self.msg = QLabel()
        self.msg.setWordWrap(True)
        self.msg.setStyleSheet(f"color: {RED}; font-size: 12px;")
        root.addWidget(self.msg)

        foot = QHBoxLayout()
        foot.addStretch(1)
        cancel = QPushButton("Mégse")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Mentés", objectName="primary")
        save.clicked.connect(self._save)
        save.setDefault(True)
        foot.addWidget(cancel)
        foot.addWidget(save)
        root.addLayout(foot)

    def _add_row(self, number: str = "", name: str = "") -> int:
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(number))
        self.table.setItem(r, 1, QTableWidgetItem(name))
        return r

    def _new_row(self):
        if self.table.rowCount() >= MAX_EXTENSIONS:
            self.msg.setText(f"Legfeljebb {MAX_EXTENSIONS} mellék menthető.")
            return
        r = self._add_row()
        self.table.setCurrentCell(r, 0)
        self.table.editItem(self.table.item(r, 0))

    def _remove(self):
        r = self.table.currentRow()
        if r >= 0:
            self.table.removeRow(r)

    def _move(self, d: int):
        r = self.table.currentRow()
        t = r + d
        if r < 0 or not (0 <= t < self.table.rowCount()):
            return
        a = [self.table.item(r, c).text() if self.table.item(r, c) else "" for c in (0, 1)]
        b = [self.table.item(t, c).text() if self.table.item(t, c) else "" for c in (0, 1)]
        for c in (0, 1):
            self.table.item(r, c).setText(b[c])
            self.table.item(t, c).setText(a[c])
        self.table.setCurrentCell(t, self.table.currentColumn())

    def _save(self):
        self.table.setCurrentCell(-1, -1)            # commit an open editor
        items, bad, seen = [], [], set()
        for r in range(self.table.rowCount()):
            number = self.table.item(r, 0).text() if self.table.item(r, 0) else ""
            name = self.table.item(r, 1).text() if self.table.item(r, 1) else ""
            if not number.strip() and not name.strip():
                continue                               # empty row: ignored
            ok = clean_extension(number, name)
            for c in (0, 1):
                if self.table.item(r, c):
                    self.table.item(r, c).setBackground(QColor(0, 0, 0, 0))
            if not ok or ok[0] in seen:
                bad.append(r)
                for c in (0, 1):
                    if self.table.item(r, c):
                        self.table.item(r, c).setBackground(QColor(243, 139, 168, 60))
                continue
            seen.add(ok[0])
            items.append(ok)
        if bad:
            self.msg.setText("A pirossal jelölt sor hibás: a mellék csak számjegyeket (és * # +) tartalmazhat, a név "
                             "nem lehet üres, és egy mellék csak egyszer szerepelhet.")
            return
        self.settings.set_extensions(items)
        self.accept()
