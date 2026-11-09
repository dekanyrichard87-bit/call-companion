"""Status window for Call Companion (0.3 milestone 1).

Self-contained: a small data model (StatusModel) that the rest of the app feeds,
and a dark StatusWindow that renders it. The window has no dependency on
main.py / crm.py internals. The only coupling is:

  * the controller / detector / crm / local_api call the StatusModel methods,
  * "Simulate call" emits simulate_requested(number, scenario); main.py feeds it
    into the real pipeline (card, lookup, cache, call log),
  * "Re-check MiniCRM" emits recheck_requested; the controller answers by emitting
    StatusModel.recheck_finished once its session check has completed.

Run `python status_window.py` for a standalone preview with sample data.
"""
from __future__ import annotations

import html
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from PySide6.QtCore import QObject, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QFrame, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QPushButton, QSystemTrayIcon, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

log = logging.getLogger("callcompanion.status")

# ----------------------------------------------------------------- palette --
BASE, SURFACE, SURFACE2 = "#1e1e2e", "#313244", "#45475a"
TEXT, SUBTEXT = "#cdd6f4", "#a6adc8"
GREEN, YELLOW, RED, BLUE = "#a6e3a1", "#f9e2af", "#f38ba8", "#89b4fa"


# ------------------------------------------------------------------- model --
class Health(Enum):
    OK = "ok"
    WARN = "warn"
    ERROR = "error"
    UNKNOWN = "unknown"


HEALTH_COLOR = {Health.OK: GREEN, Health.WARN: YELLOW,
                Health.ERROR: RED, Health.UNKNOWN: SUBTEXT}

STATUS_HU = {"ringing": "Csörög", "answered": "Fogadva", "ended": "Befejezve",
             "missed": "Nem fogadott", "rejected": "Elutasítva"}
STATUS_COLOR = {"ringing": BLUE, "answered": GREEN, "ended": SUBTEXT,
                "missed": RED, "rejected": YELLOW}


@dataclass
class HealthItem:
    state: Health = Health.UNKNOWN
    text: str = "Ellenőrzés…"


@dataclass
class CallRecord:
    call_id: str
    number: str
    started: datetime
    status: str = "ringing"          # ringing / answered / ended / missed / rejected
    name: str | None = None
    lookup: str = "pending"          # pending / found / unknown / failed
    detail: str = ""
    profile_url: str | None = None
    elapsed_ms: int = 0


class StatusModel(QObject):
    """Everything the status window shows. Call these from the Qt main thread
    (or via queued signals); each mutation emits `changed` once."""

    changed = Signal()
    recheck_finished = Signal()      # emitted by main.Controller.on_session after every session check
    MAX_CALLS = 50

    def __init__(self) -> None:
        super().__init__()
        self.user_name: str | None = None
        self.minicrm = HealthItem()
        self.bria = HealthItem()
        self.extension = HealthItem()
        self.api = HealthItem()
        self.calls: list[CallRecord] = []

    # -- health ---------------------------------------------------------
    def set_user_name(self, name: str | None) -> None:
        name = (name or "").strip() or None
        if name != self.user_name:
            self.user_name = name
            self.changed.emit()

    def _set_health(self, attr: str, state: Health, text: str) -> None:
        new = HealthItem(state, text)
        if getattr(self, attr) != new:
            setattr(self, attr, new)
            self.changed.emit()

    def set_minicrm(self, state: Health, text: str) -> None:
        self._set_health("minicrm", state, text)

    def set_bria(self, state: Health, text: str) -> None:
        self._set_health("bria", state, text)

    def set_extension(self, state: Health, text: str) -> None:
        self._set_health("extension", state, text)

    def set_api(self, state: Health, text: str) -> None:
        self._set_health("api", state, text)

    # -- calls ----------------------------------------------------------
    def _find(self, call_id: str) -> CallRecord | None:
        return next((c for c in self.calls if c.call_id == call_id), None)

    def call_ringing(self, call_id: str, number: str) -> None:
        if self._find(call_id):
            return
        self.calls.insert(0, CallRecord(call_id, number, datetime.now()))
        del self.calls[self.MAX_CALLS:]
        self.changed.emit()

    def call_resolved(self, call_id: str, name: str | None,
                      profile_url: str | None = None, state: str = "found",
                      detail: str = "", elapsed_ms: int = 0) -> None:
        rec = self._find(call_id)
        if rec:
            rec.name, rec.profile_url = name, profile_url
            rec.lookup, rec.detail, rec.elapsed_ms = state, detail, elapsed_ms
            self.changed.emit()

    def call_answered(self, call_id: str) -> None:
        rec = self._find(call_id)
        if rec:
            rec.status = "answered"
            self.changed.emit()

    def call_ended(self, call_id: str, rejected: bool = False) -> None:
        rec = self._find(call_id)
        if not rec:
            return
        if rec.status == "ringing":          # never answered -> missed / rejected
            rec.status = "rejected" if rejected else "missed"
        elif rec.status == "answered":
            rec.status = "ended"
        self.changed.emit()


# ------------------------------------------------------------------ window --
QSS = f"""
QWidget {{ background: {BASE}; color: {TEXT}; font-family: "Segoe UI"; font-size: 13px; }}
QFrame#card {{ background: {SURFACE}; border-radius: 12px; }}
QLabel#section {{ color: {SUBTEXT}; font-size: 11px; font-weight: 600; letter-spacing: 1px; }}
QLabel#greeting {{ font-size: 17px; font-weight: 600; background: transparent; }}
QLabel {{ background: transparent; }}
QTableWidget {{ background: {SURFACE}; border: none; border-radius: 12px; gridline-color: {SURFACE};
               selection-background-color: {SURFACE2}; selection-color: {TEXT}; }}
QHeaderView::section {{ background: {SURFACE}; color: {SUBTEXT}; border: none; padding: 6px; font-size: 11px; }}
QLineEdit, QComboBox {{ background: {BASE}; border: 1px solid {SURFACE2}; border-radius: 8px; padding: 6px 8px; }}
QComboBox QAbstractItemView {{ background: {SURFACE}; selection-background-color: {SURFACE2}; }}
QPushButton {{ background: {SURFACE2}; border: none; border-radius: 8px; padding: 7px 14px; }}
QPushButton:hover {{ background: #585b70; }}
QPushButton#primary {{ background: {BLUE}; color: {BASE}; font-weight: 600; }}
QPushButton#primary:hover {{ background: #a6c8ff; }}
QPushButton:disabled {{ color: {SUBTEXT}; background: {SURFACE}; }}
"""

RECHECK_LABEL = "MiniCRM újraellenőrzése"
RECHECK_TIMEOUT_MS = 8000


class StatusWindow(QWidget):
    recheck_requested = Signal()
    simulate_requested = Signal(str, str)     # number, scenario (missed / answered / long)

    def __init__(self, model: StatusModel, data_dir: str | None = None) -> None:
        super().__init__()
        self.model = model
        self._really_close = False
        self._tray: QSystemTrayIcon | None = None
        self._recheck_pending = False
        self._recheck_token = 0
        self._recheck_t0 = 0.0
        self.data_dir = data_dir or os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "CallCompanion")

        self.setWindowTitle("Call Companion")
        self.setMinimumSize(520, 520)
        self.setStyleSheet(QSS)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        # header
        self.greeting = QLabel(objectName="greeting")
        self.greeting.setTextFormat(Qt.RichText)
        root.addWidget(self.greeting)

        # health
        root.addWidget(self._section("RENDSZERÁLLAPOT"))
        card = QFrame(objectName="card")
        grid = QVBoxLayout(card)
        grid.setContentsMargins(14, 10, 14, 10)
        grid.setSpacing(6)
        self._rows: dict[str, tuple[QLabel, QLabel]] = {}
        for key, title in (("bria", "Bria naplófigyelés"),
                           ("minicrm", "MiniCRM munkamenet"),
                           ("extension", "Chrome bővítmény"),
                           ("api", "Helyi API")):
            row = QHBoxLayout()
            dot = QLabel("●")
            dot.setFixedWidth(18)
            name = QLabel(title)
            name.setFixedWidth(150)
            detail = QLabel()
            detail.setStyleSheet(f"color: {SUBTEXT};")
            detail.setWordWrap(True)
            row.addWidget(dot)
            row.addWidget(name)
            row.addWidget(detail, 1)
            grid.addLayout(row)
            self._rows[key] = (dot, detail)
        root.addWidget(card)

        # call history
        root.addWidget(self._section("LEGUTÓBBI HÍVÁSOK"))
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Idő", "Szám", "Név", "Állapot"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setShowGrid(False)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.cellDoubleClicked.connect(self._open_profile)
        self.table.setToolTip("Dupla kattintás: névjegy megnyitása a MiniCRM-ben")
        root.addWidget(self.table, 1)
        self.empty = QLabel("Még nem volt hívás ebben a munkamenetben.")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setStyleSheet(f"color: {SUBTEXT};")
        root.addWidget(self.empty)

        # simulate
        root.addWidget(self._section("HÍVÁS SZIMULÁLÁSA"))
        sim = QHBoxLayout()
        self.sim_number = QLineEdit()
        self.sim_number.setPlaceholderText("pl. 06301234567 vagy +36301234567")
        self.sim_scenario = QComboBox()
        self.sim_scenario.addItem("Nem fogadott (6 mp)", "missed")
        self.sim_scenario.addItem("Fogadott (4 mp után)", "answered")
        self.sim_scenario.addItem("Hosszú csörgés (15 mp)", "long")
        self.sim_button = QPushButton("Szimulálás", objectName="primary")
        self.sim_button.clicked.connect(self._simulate)
        self.sim_number.returnPressed.connect(self._simulate)
        sim.addWidget(self.sim_number, 1)
        sim.addWidget(self.sim_scenario)
        sim.addWidget(self.sim_button)
        root.addLayout(sim)
        self.sim_result = QLabel()
        self.sim_result.setStyleSheet(f"color: {SUBTEXT}; font-size: 12px;")
        self.sim_result.setWordWrap(True)
        root.addWidget(self.sim_result)

        # re-check feedback line (above the footer buttons)
        self.recheck_result = QLabel()
        self.recheck_result.setWordWrap(True)
        self.recheck_result.setStyleSheet(f"color: {SUBTEXT}; font-size: 12px;")
        root.addWidget(self.recheck_result)

        # footer
        foot = QHBoxLayout()
        self.recheck_btn = QPushButton(RECHECK_LABEL)
        self.recheck_btn.clicked.connect(self._recheck_clicked)
        logs = QPushButton("Naplómappa megnyitása")
        logs.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(self.data_dir)))
        foot.addWidget(self.recheck_btn)
        foot.addWidget(logs)
        foot.addStretch(1)
        root.addLayout(foot)

        model.changed.connect(self.refresh)
        model.recheck_finished.connect(self._recheck_done)
        self.refresh()

    # ------------------------------------------------------------ helpers --
    @staticmethod
    def _section(text: str) -> QLabel:
        lab = QLabel(text)
        lab.setObjectName("section")
        return lab

    def set_tray(self, tray: QSystemTrayIcon) -> None:
        """Remember the tray icon so the window can be placed next to it."""
        self._tray = tray

    def _placement_screen(self):
        # tray.geometry() is often empty on Windows 11 (icon in the overflow area): fall back to the
        # screen under the cursor (the user has just double-clicked the tray), then the primary screen.
        if self._tray is not None:
            g = self._tray.geometry()
            if g.isValid() and not g.isEmpty():
                s = QGuiApplication.screenAt(g.center())
                if s:
                    return s
        return QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()

    def place_near_tray(self, margin: int = 12) -> None:
        """Bottom-right corner of the usable screen area = just above the taskbar / clock."""
        screen = self._placement_screen()
        area = screen.availableGeometry()          # excludes the taskbar
        fg = self.frameGeometry()
        x = max(area.left(), area.right() - fg.width() + 1 - margin)
        y = max(area.top(), area.bottom() - fg.height() + 1 - margin)
        self.move(x, y)

    def show_and_raise(self) -> None:
        was_hidden = not self.isVisible()
        self.refresh()
        if was_hidden:
            self.place_near_tray()
        self.showNormal()
        if was_hidden:
            self.place_near_tray()                 # the frame size is exact only after the first show
            log.info("status window placed at %d,%d (screen %s)", self.x(), self.y(),
                     self._placement_screen().name())
        self.raise_()
        self.activateWindow()

    def prepare_quit(self) -> None:
        """Call before app.quit(): from now on closing really closes (no hide-to-tray)."""
        self._really_close = True

    def closeEvent(self, event) -> None:
        # The X button hides to the tray; programmatic closes (quit, session end) go through.
        if self._really_close or not event.spontaneous():
            event.accept()
            return
        event.ignore()
        self.hide()

    # ------------------------------------------------------------ refresh --
    def refresh(self) -> None:
        m = self.model
        # greeting
        who = f", {html.escape(m.user_name)}" if m.user_name else ""
        mc = m.minicrm
        word = {Health.OK: "Aktív", Health.WARN: "Figyelmeztetés",
                Health.ERROR: "Bejelentkezés szükséges",
                Health.UNKNOWN: "Ellenőrzés…"}[mc.state]
        self.greeting.setText(
            f"Üdvözöljük{who}! <span style='color:{SUBTEXT}'>•</span> "
            f"<span style='color:{HEALTH_COLOR[mc.state]}'>MiniCRM: {word}</span>")

        # health rows
        for key, item in (("bria", m.bria), ("minicrm", m.minicrm),
                          ("extension", m.extension), ("api", m.api)):
            dot, detail = self._rows[key]
            dot.setStyleSheet(f"color: {HEALTH_COLOR[item.state]}; font-size: 14px;")
            detail.setText(item.text)

        # call table
        self.empty.setVisible(not m.calls)
        self.table.setVisible(bool(m.calls))
        self.table.setRowCount(len(m.calls))
        today = datetime.now().date()
        for r, c in enumerate(m.calls):
            stamp = c.started.strftime("%H:%M:%S" if c.started.date() == today
                                       else "%m.%d. %H:%M")
            if c.name:
                name, name_color = c.name, TEXT
            elif c.lookup == "pending":
                name, name_color = "Keresés…", SUBTEXT
            elif c.lookup == "failed":
                name, name_color = f"Keresés sikertelen{': ' + c.detail if c.detail else ''}", YELLOW
            else:
                name, name_color = "Ismeretlen hívó", SUBTEXT
            cells = (stamp, c.number, name, STATUS_HU.get(c.status, c.status))
            colors = (SUBTEXT, TEXT, name_color, STATUS_COLOR.get(c.status, TEXT))
            for col, (txt, colr) in enumerate(zip(cells, colors)):
                it = QTableWidgetItem(txt)
                it.setForeground(QColor(colr))
                if col == 2 and c.elapsed_ms:
                    it.setToolTip(f"MiniCRM keresés: {c.elapsed_ms} ms")
                self.table.setItem(r, col, it)

    # ------------------------------------------------------------ actions --
    def _open_profile(self, row: int, _col: int) -> None:
        if 0 <= row < len(self.model.calls):
            url = self.model.calls[row].profile_url
            if url:
                QDesktopServices.openUrl(QUrl(url))

    def _simulate(self) -> None:
        number = self.sim_number.text().strip()
        if sum(ch.isdigit() for ch in number) < 6:
            self._sim_msg("Adjon meg legalább 6 számjegyű telefonszámot.", RED)
            return
        self.simulate_requested.emit(number, self.sim_scenario.currentData())
        self._sim_msg(f"Szimulált hívás elindítva: {number} "
                      f"({self.sim_scenario.currentText()}).", GREEN)

    def _sim_msg(self, text: str, color: str) -> None:
        self.sim_result.setStyleSheet(f"color: {color}; font-size: 12px;")
        self.sim_result.setText(text)

    # ---------------------------------------------------- re-check feedback --
    def _recheck_clicked(self) -> None:
        if self._recheck_pending:
            return
        self._recheck_pending = True
        self._recheck_t0 = time.monotonic()
        self._recheck_token += 1
        token = self._recheck_token
        self.recheck_btn.setEnabled(False)
        self.recheck_btn.setText("Ellenőrzés…")
        ext = self.model.extension.state
        log.info("recheck: requested (extension=%s)", ext.value)
        if ext in (Health.UNKNOWN, Health.WARN):
            self._recheck_msg("Nincs aktív Chrome bővítmény – nyissa meg a Chrome-ot a "
                              "bejelentkezett MiniCRM-mel, majd próbálja újra.", YELLOW)
        else:
            self._recheck_msg("Ellenőrzés folyamatban…", SUBTEXT)
        self.recheck_requested.emit()
        QTimer.singleShot(RECHECK_TIMEOUT_MS, lambda: self._recheck_timeout(token))

    def _recheck_done(self) -> None:
        if not self._recheck_pending:            # background session checks are not ours
            return
        mc, ext = self.model.minicrm, self.model.extension
        no_ext = ext.state in (Health.UNKNOWN, Health.WARN)
        if no_ext and mc.state is not Health.OK:
            # keep the actionable hint instead of the short status text
            text = ("Nincs aktív Chrome bővítmény – nyissa meg a Chrome-ot a "
                    "bejelentkezett MiniCRM-mel, majd próbálja újra.")
        else:
            text = mc.text
        color = HEALTH_COLOR[mc.state]
        log.info("recheck: done minicrm=%s extension=%s", mc.state.value, ext.state.value)
        token = self._recheck_token
        # hold the busy state for >= 700 ms so a near-instant answer still looks like a click registered
        left = max(0, 700 - int((time.monotonic() - self._recheck_t0) * 1000))

        def finish():
            if self._recheck_pending and token == self._recheck_token:
                self._recheck_finish(text, color)
        QTimer.singleShot(left, finish)

    def _recheck_timeout(self, token: int) -> None:
        if self._recheck_pending and token == self._recheck_token:
            log.warning("recheck: no result after %d s", RECHECK_TIMEOUT_MS // 1000)
            self._recheck_finish("Nem érkezett válasz az ellenőrzésre (időtúllépés).", YELLOW)

    def _recheck_finish(self, text: str, color: str) -> None:
        self._recheck_pending = False
        self.recheck_btn.setEnabled(True)
        self.recheck_btn.setText(RECHECK_LABEL)
        self._recheck_msg(text, color)

    def _recheck_msg(self, text: str, color: str) -> None:
        self.recheck_result.setStyleSheet(f"color: {color}; font-size: 12px;")
        self.recheck_result.setText(text)


# -------------------------------------------------------------------- tray --
def attach_to_tray(tray: QSystemTrayIcon, window: StatusWindow) -> None:
    """Double-click (or single click on some setups) on the tray icon opens the window."""
    window.set_tray(tray)

    def on_activated(reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.DoubleClick:
            window.show_and_raise()
    tray.activated.connect(on_activated)


# ---------------------------------------------------------- standalone demo --
def _demo() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    model = StatusModel()
    win = StatusWindow(model)
    win.prepare_quit()  # let the demo quit
    model.set_user_name("Dékány Richárd")
    model.set_minicrm(Health.OK, "Bejelentkezve, a munkamenet érvényes")
    model.set_bria(Health.OK, "Log.txt figyelése, utolsó esemény 08:57:12")
    model.set_extension(Health.OK, "Utolsó szinkron 08:58 (14 süti)")
    model.set_api(Health.OK, "127.0.0.1:48653")
    model.call_ringing("a", "+36301234567")
    model.call_resolved("a", "Minta Anna", "https://r3.minicrm.hu/60171/#AddressBook/1")
    model.call_answered("a"); model.call_ended("a")
    model.call_ringing("b", "+4915123456789")
    model.call_resolved("b", None, state="unknown"); model.call_ended("b")
    model.call_ringing("c", "06201112233")
    win.show_and_raise()
    return app.exec()


if __name__ == "__main__":
    sys.exit(_demo())
