"""Extension list editor (v0.8.0, safer since v0.9.0): the colleagues a call can be transferred to from the card.

Saved by settings.Settings in %LOCALAPPDATA%\\CallCompanion\\settings.json, so each company / PC keeps its own list.
v0.9.0: the table is read-only (one click only selects a row, nothing can be overwritten by accident). Adding and
editing happen in a small form under the list (+ Új mellék / ✎ Szerkesztés -> Mellék, Kolléga neve, Mentés / Mégse);
deleting asks inline. Nothing changes until Mentés / Igen. No double-clicks. Shown on the "Mellékek" page of the
settings dialog (settings_dialog.py).
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from settings import MAX_EXTENSIONS, clean_extension
from status_window import BASE, BLUE, MANTLE, QSS as MAIN_QSS, RED, SUBTEXT, SURFACE, SURFACE2, TEXT

QSS = MAIN_QSS + f"""
QTableWidget {{ background: {SURFACE}; border: none; border-radius: 10px; gridline-color: {SURFACE2};
               selection-background-color: {SURFACE2}; selection-color: {TEXT}; }}
QHeaderView::section {{ background: {SURFACE}; color: {SUBTEXT}; border: none; padding: 6px; font-size: 11px; }}
QPushButton#primary {{ background: {BLUE}; color: {BASE}; font-weight: 600; }}
QPushButton#primary:hover {{ background: #a6c8ff; }}
QPushButton#danger {{ background: #5a2a38; color: #f38ba8; font-weight: 600; }}
QPushButton#danger:hover {{ background: #6e3344; }}
QFrame#form {{ background: {MANTLE}; border-radius: 10px; }}
QLabel#err {{ color: {RED}; font-size: 11px; }}
"""


class ExtensionsEditor(QWidget):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._editing: int | None = None          # row being edited; -1 = new entry; None = form closed
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        intro = QLabel("A kollégák belső mellékei. Ha a Bria engedi az átkapcsolást, a hívás kártyáján "
                       "(↪ Átkapcsolás) ezekre lehet egy kattintással átkapcsolni, ebben a sorrendben.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {SUBTEXT}; font-size: 12px;")
        root.addWidget(intro)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Mellék", "Kolléga neve"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)     # read-only: no accidents
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 110)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._sync_buttons)
        root.addWidget(self.table, 1)
        self.empty = QLabel("Még nincs mentett mellék. Adja hozzá az elsőt: + Új mellék")
        self.empty.setStyleSheet(f"color: {SUBTEXT}; font-size: 12px;")
        root.addWidget(self.empty)

        tools = QHBoxLayout()
        self.btn_add = QPushButton("+ Új mellék")
        self.btn_add.clicked.connect(lambda: self._open_form(-1))
        self.btn_edit = QPushButton("✎ Szerkesztés")
        self.btn_edit.clicked.connect(lambda: self._open_form(self.table.currentRow()))
        self.btn_del = QPushButton("Törlés")
        self.btn_del.clicked.connect(self._ask_delete)
        self.btn_up = QPushButton("▲")
        self.btn_up.setToolTip("Feljebb (a kártyán is ebben a sorrendben jelennek meg)")
        self.btn_up.clicked.connect(lambda: self._move(-1))
        self.btn_down = QPushButton("▼")
        self.btn_down.setToolTip("Lejjebb")
        self.btn_down.clicked.connect(lambda: self._move(1))
        for b in (self.btn_add, self.btn_edit, self.btn_del, self.btn_up, self.btn_down):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            tools.addWidget(b)
        tools.addStretch(1)
        root.addLayout(tools)

        # ---- add / edit form (hidden until + Új mellék / ✎ Szerkesztés) ----
        self.form = QFrame(objectName="form")
        fl = QGridLayout(self.form)
        fl.setContentsMargins(14, 12, 14, 12)
        fl.setHorizontalSpacing(10)
        fl.setVerticalSpacing(4)
        self.form_title = QLabel()
        self.form_title.setStyleSheet("font-weight: 600;")
        fl.addWidget(self.form_title, 0, 0, 1, 2)
        fl.addWidget(QLabel("Mellék"), 1, 0)
        fl.addWidget(QLabel("Kolléga neve"), 1, 1)
        self.in_number = QLineEdit()
        self.in_number.setPlaceholderText("pl. 102")
        self.in_number.setMaximumWidth(120)
        self.in_name = QLineEdit()
        self.in_name.setPlaceholderText("pl. Kovács Péter")
        fl.addWidget(self.in_number, 2, 0)
        fl.addWidget(self.in_name, 2, 1)
        self.err_number = QLabel(objectName="err")
        self.err_name = QLabel(objectName="err")
        fl.addWidget(self.err_number, 3, 0)
        fl.addWidget(self.err_name, 3, 1)
        frow = QHBoxLayout()
        frow.addStretch(1)
        cancel = QPushButton("Mégse")
        cancel.clicked.connect(self._close_form)
        self.btn_save = QPushButton("Mentés", objectName="primary")
        self.btn_save.clicked.connect(self._save_form)
        frow.addWidget(cancel)
        frow.addWidget(self.btn_save)
        fl.addLayout(frow, 4, 0, 1, 2)
        fl.setColumnStretch(1, 1)
        for w in (self.in_number, self.in_name):
            w.returnPressed.connect(self._save_form)
        root.addWidget(self.form)
        self.form.setVisible(False)

        # ---- inline delete confirmation ----
        self.confirm = QFrame(objectName="form")
        cl = QHBoxLayout(self.confirm)
        cl.setContentsMargins(14, 10, 14, 10)
        self.confirm_text = QLabel()
        cl.addWidget(self.confirm_text, 1)
        no = QPushButton("Mégse")
        no.clicked.connect(lambda: self.confirm.setVisible(False))
        yes = QPushButton("Igen, törlöm", objectName="danger")
        yes.clicked.connect(self._delete)
        cl.addWidget(no)
        cl.addWidget(yes)
        root.addWidget(self.confirm)
        self.confirm.setVisible(False)

        self._reload()

    # ---- list ------------------------------------------------------------------------------------------------------
    def _items(self) -> list[tuple[str, str]]:
        return list(self.settings.extensions)

    def _reload(self, select: int | None = None):
        items = self._items()
        self.table.setRowCount(0)
        for number, name in items:
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(number))
            self.table.setItem(r, 1, QTableWidgetItem(name))
        if select is not None and 0 <= select < len(items):
            self.table.selectRow(select)
        else:
            self.table.clearSelection()
        self.table.setVisible(bool(items))
        self.empty.setVisible(not items)
        self._sync_buttons()

    def _sync_buttons(self):
        r = self.table.currentRow() if self.table.selectedItems() else -1
        n = self.table.rowCount()
        busy = self.form.isVisible()
        self.btn_add.setEnabled(not busy and n < MAX_EXTENSIONS)
        self.btn_add.setToolTip("" if n < MAX_EXTENSIONS else f"Legfeljebb {MAX_EXTENSIONS} mellék menthető.")
        self.btn_edit.setEnabled(not busy and r >= 0)
        self.btn_del.setEnabled(not busy and r >= 0)
        self.btn_up.setEnabled(not busy and r > 0)
        self.btn_down.setEnabled(not busy and 0 <= r < n - 1)

    def _store(self, items, select: int | None):
        self.settings.set_extensions(items)
        self._reload(select)

    # ---- form ------------------------------------------------------------------------------------------------------
    def _open_form(self, row: int):
        self.confirm.setVisible(False)
        items = self._items()
        self._editing = row
        if row >= 0 and row < len(items):
            number, name = items[row]
            self.form_title.setText(f"Mellék szerkesztése: {name} ({number})")
        else:
            self._editing, number, name = -1, "", ""
            self.form_title.setText("Új mellék")
        self.in_number.setText(number)
        self.in_name.setText(name)
        self.err_number.clear()
        self.err_name.clear()
        self.form.setVisible(True)
        self._sync_buttons()
        self.in_number.setFocus()

    def _close_form(self):
        self.form.setVisible(False)
        self._editing = None
        self._sync_buttons()

    def _save_form(self):
        if self._editing is None:
            return
        number, name = self.in_number.text().strip(), self.in_name.text().strip()
        items = self._items()
        self.err_number.clear()
        self.err_name.clear()
        ok = clean_extension(number, name)
        num_ok = clean_extension(number, "x")          # the number alone (the name is checked separately)
        others = [n for i, (n, _nm) in enumerate(items) if i != self._editing]
        if not number:
            self.err_number.setText("Adja meg a mellékszámot.")
        elif not num_ok:
            self.err_number.setText("Csak számjegy, *, # vagy + lehet.")
        elif num_ok[0] in others:
            self.err_number.setText("Ez a mellék már szerepel.")
        if not name:
            self.err_name.setText("Adja meg a kolléga nevét.")
        if self.err_number.text() or self.err_name.text() or not ok:
            return
        if self._editing >= 0:
            items[self._editing] = ok
            sel = self._editing
        else:
            items.append(ok)
            sel = len(items) - 1
        self.form.setVisible(False)
        self._editing = None
        self._store(items, sel)

    # ---- delete / order --------------------------------------------------------------------------------------------
    def _ask_delete(self):
        r = self.table.currentRow()
        items = self._items()
        if not (0 <= r < len(items)):
            return
        number, name = items[r]
        self.confirm_text.setText(f"Biztosan törli: {name} ({number})?")
        self._del_row = r
        self.confirm.setVisible(True)

    def _delete(self):
        r = getattr(self, "_del_row", None)
        items = self._items()
        self.confirm.setVisible(False)
        if isinstance(r, int) and 0 <= r < len(items):
            del items[r]
            self._store(items, min(r, len(items) - 1) if items else None)

    def _move(self, d: int):
        r = self.table.currentRow()
        items = self._items()
        t = r + d
        if not (0 <= r < len(items)) or not (0 <= t < len(items)):
            return
        items[r], items[t] = items[t], items[r]
        self._store(items, t)
