"""Developer tools window (v0.6.0): everything technical, in one place, out of the normal user's way.

Opened from the tray menu or the main window only while developer mode is on. Contents:
  * detailed health of every part (Bria log, MiniCRM session, Chrome extension, local API, Bria control API),
  * MiniCRM re-check with feedback, Bria control API check,
  * call simulator (runs through the real pipeline: card, lookup, history),
  * live view of app.log (tailed only while the window is visible), log folder, update check, history reset.
"""
from __future__ import annotations

import logging
import os
import time

from PySide6.QtCore import QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices, QFont, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
    QVBoxLayout, QWidget,
)

from status_window import (BASE, GREEN, HEALTH_COLOR, MANTLE, QSS as MAIN_QSS, RED, SUBTEXT, SURFACE, YELLOW,
                           Health, StatusModel)

log = logging.getLogger("callcompanion.dev")

RECHECK_LABEL = "MiniCRM újraellenőrzése"
RECHECK_TIMEOUT_MS = 8000
LOG_TAIL_BYTES = 64 * 1024
LOG_MAX_LINES = 600

QSS = MAIN_QSS + f"""
QFrame#card {{ background: {SURFACE}; border-radius: 12px; }}
QComboBox {{ background: {MANTLE}; border: 1px solid #45475a; border-radius: 8px; padding: 6px 8px; }}
QComboBox QAbstractItemView {{ background: {SURFACE}; selection-background-color: #45475a; }}
QPlainTextEdit {{ background: {MANTLE}; border: none; border-radius: 10px; color: #bac2de;
                 font-family: Consolas, "Cascadia Mono", monospace; font-size: 11px; padding: 6px; }}
QPushButton#primary {{ background: #89b4fa; color: {BASE}; font-weight: 600; }}
QPushButton#primary:hover {{ background: #a6c8ff; }}
"""


class DevWindow(QWidget):
    recheck_requested = Signal()
    simulate_requested = Signal(str, str)       # number, scenario
    check_updates = Signal()
    bria_api_check = Signal()                   # v0.8.0

    def __init__(self, model: StatusModel, data_dir: str, version: str = "") -> None:
        super().__init__()
        self.model = model
        self.data_dir = data_dir
        self.log_path = os.path.join(data_dir, "app.log")
        self._log_pos = 0
        self._recheck_pending = False
        self._recheck_token = 0
        self._recheck_t0 = 0.0
        self._really_close = False

        self.setWindowTitle(f"Call Companion – Fejlesztői eszközök {version}")
        self.setMinimumSize(720, 680)
        self.resize(820, 760)
        self.setStyleSheet(QSS)
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        # ---- health ----
        root.addWidget(self._section("RÉSZLETES ÁLLAPOT"))
        card = QFrame(objectName="card")
        grid = QVBoxLayout(card)
        grid.setContentsMargins(14, 10, 14, 10)
        grid.setSpacing(6)
        self._rows: dict[str, QLabel] = {}
        self._dots: dict[str, QLabel] = {}
        for key, title in (("bria", "Bria naplófigyelés"), ("minicrm", "MiniCRM munkamenet"),
                           ("extension", "Chrome bővítmény"), ("api", "Helyi API"),
                           ("bria_api", "Bria vezérlés (API)")):
            row = QHBoxLayout()
            dot = QLabel("●")
            dot.setFixedWidth(18)
            name = QLabel(title)
            name.setFixedWidth(170)
            detail = QLabel()
            detail.setStyleSheet(f"color: {SUBTEXT};")
            detail.setWordWrap(True)
            detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            row.addWidget(dot)
            row.addWidget(name)
            row.addWidget(detail, 1)
            grid.addLayout(row)
            self._rows[key], self._dots[key] = detail, dot
        root.addWidget(card)

        checks = QHBoxLayout()
        self.recheck_btn = QPushButton(RECHECK_LABEL)
        self.recheck_btn.clicked.connect(self._recheck_clicked)
        self.bria_btn = QPushButton("Bria vezérlés ellenőrzése")
        self.bria_btn.setToolTip("Megkérdezi a Briát a futó hívásokról (Bria Desktop API).")
        self.bria_btn.clicked.connect(self.bria_api_check.emit)
        upd = QPushButton("Frissítések keresése")
        upd.clicked.connect(self.check_updates.emit)
        checks.addWidget(self.recheck_btn)
        checks.addWidget(self.bria_btn)
        checks.addWidget(upd)
        checks.addStretch(1)
        root.addLayout(checks)
        self.recheck_result = QLabel()
        self.recheck_result.setWordWrap(True)
        self.recheck_result.setStyleSheet(f"color: {SUBTEXT}; font-size: 12px;")
        root.addWidget(self.recheck_result)

        # ---- simulator ----
        root.addWidget(self._section("HÍVÁS SZIMULÁLÁSA"))
        sim = QHBoxLayout()
        self.sim_number = QLineEdit()
        self.sim_number.setPlaceholderText("pl. 06301234567 vagy +36301234567")
        self.sim_scenario = QComboBox()
        for label, key in (("Nem fogadott (6 mp)", "missed"), ("Fogadott (4 mp után, 20 mp hívás)", "answered"),
                           ("Hosszú csörgés (15 mp)", "long"), ("Kimenő hívás (4 mp után fogadják)", "outgoing"),
                           ("Kimenő, nem fogadják (8 mp)", "outgoing_unanswered")):
            self.sim_scenario.addItem(label, key)
        self.sim_button = QPushButton("Szimulálás", objectName="primary")
        self.sim_button.clicked.connect(self._simulate)
        self.sim_number.returnPressed.connect(self._simulate)
        sim.addWidget(self.sim_number, 1)
        sim.addWidget(self.sim_scenario)
        sim.addWidget(self.sim_button)
        root.addLayout(sim)
        self.sim_result = QLabel()
        self.sim_result.setStyleSheet(f"color: {SUBTEXT}; font-size: 12px;")
        root.addWidget(self.sim_result)

        # ---- live log ----
        lh = QHBoxLayout()
        lh.addWidget(self._section("ÉLŐ NAPLÓ (app.log)"))
        lh.addStretch(1)
        self.follow = QCheckBox("Görgetés követése")
        self.follow.setChecked(True)
        lh.addWidget(self.follow)
        root.addLayout(lh)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(LOG_MAX_LINES)
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log_view.setFont(QFont("Consolas", 9))
        root.addWidget(self.log_view, 1)

        foot = QHBoxLayout()
        logs = QPushButton("Naplómappa megnyitása")
        logs.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.data_dir)))
        clear = QPushButton("Híváselőzmények törlése…")
        clear.clicked.connect(self._clear_history)
        foot.addWidget(logs)
        foot.addWidget(clear)
        foot.addStretch(1)
        root.addLayout(foot)

        self._log_timer = QTimer(self)
        self._log_timer.setInterval(1000)
        self._log_timer.timeout.connect(self._tail_log)
        model.changed.connect(self._refresh_health)
        model.recheck_finished.connect(self._recheck_done)
        self._refresh_health()

    @staticmethod
    def _section(text: str) -> QLabel:
        lab = QLabel(text)
        lab.setObjectName("section")
        return lab

    # ---- window ----------------------------------------------------------------------------------------------
    def show_and_raise(self) -> None:
        self._refresh_health()
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def prepare_quit(self) -> None:
        self._really_close = True

    def closeEvent(self, event) -> None:
        if self._really_close or not event.spontaneous():
            event.accept()
            return
        event.ignore()
        self.hide()

    def showEvent(self, e) -> None:
        self._log_pos = 0
        self.log_view.clear()
        self._tail_log()
        self._log_timer.start()
        super().showEvent(e)

    def hideEvent(self, e) -> None:
        self._log_timer.stop()         # no file reads while nobody looks
        super().hideEvent(e)

    # ---- health ----------------------------------------------------------------------------------------------
    def _refresh_health(self) -> None:
        if not self.isVisible() and self._rows["bria"].text():
            return
        m = self.model
        for key, item in (("bria", m.bria), ("minicrm", m.minicrm), ("extension", m.extension),
                          ("api", m.api), ("bria_api", m.bria_api)):
            self._dots[key].setStyleSheet(f"color: {HEALTH_COLOR[item.state]}; font-size: 14px;")
            self._rows[key].setText(item.text)

    def bria_check_result(self, ok: bool, text: str) -> None:
        self._msg(self.recheck_result, text, GREEN if ok else YELLOW)

    # ---- log tail --------------------------------------------------------------------------------------------
    def _tail_log(self) -> None:
        try:
            size = os.path.getsize(self.log_path)
        except OSError:
            return
        if size < self._log_pos:                      # rotated / truncated
            self._log_pos = 0
            self.log_view.clear()
        if size == self._log_pos:
            return
        start = max(self._log_pos, size - LOG_TAIL_BYTES)
        try:
            with open(self.log_path, "rb") as fh:
                fh.seek(start)
                data = fh.read(size - start)
        except OSError:
            return
        skipped = start > self._log_pos               # jumped ahead: the first line may be cut in the middle
        self._log_pos = size
        text = data.decode("utf-8", errors="replace")
        if skipped and "\n" in text:
            text = text.split("\n", 1)[1]
        bar = self.log_view.verticalScrollBar()
        self.log_view.moveCursor(QTextCursor.MoveOperation.End)
        self.log_view.insertPlainText(text)
        if self.follow.isChecked():
            bar.setValue(bar.maximum())

    # ---- simulator -------------------------------------------------------------------------------------------
    def _simulate(self) -> None:
        number = self.sim_number.text().strip()
        if sum(ch.isdigit() for ch in number) < 6:
            self._msg(self.sim_result, "Adjon meg legalább 6 számjegyű telefonszámot.", RED)
            return
        self.simulate_requested.emit(number, self.sim_scenario.currentData())
        self._msg(self.sim_result, f"Szimulált hívás elindítva: {number} ({self.sim_scenario.currentText()}).",
                  GREEN)

    @staticmethod
    def _msg(label: QLabel, text: str, color: str) -> None:
        label.setStyleSheet(f"color: {color}; font-size: 12px;")
        label.setText(text)

    def _clear_history(self) -> None:
        if QMessageBox.question(self, "Híváselőzmények törlése",
                                "Biztosan törli az összes mentett hívást? Ez nem vonható vissza.") \
                == QMessageBox.StandardButton.Yes:
            self.model.clear_history()
            log.info("call history cleared from the developer window")

    # ---- MiniCRM re-check feedback ---------------------------------------------------------------------------
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
            self._msg(self.recheck_result, "Nincs aktív Chrome bővítmény – nyissa meg a Chrome-ot a "
                                           "bejelentkezett MiniCRM-mel, majd próbálja újra.", YELLOW)
        else:
            self._msg(self.recheck_result, "Ellenőrzés folyamatban…", SUBTEXT)
        self.recheck_requested.emit()
        QTimer.singleShot(RECHECK_TIMEOUT_MS, lambda: self._recheck_timeout(token))

    def _recheck_done(self) -> None:
        if not self._recheck_pending:
            return
        mc, ext = self.model.minicrm, self.model.extension
        if ext.state in (Health.UNKNOWN, Health.WARN) and mc.state is not Health.OK:
            text = ("Nincs aktív Chrome bővítmény – nyissa meg a Chrome-ot a bejelentkezett MiniCRM-mel, majd "
                    "próbálja újra.")
        else:
            text = mc.text
        color = HEALTH_COLOR[mc.state]
        log.info("recheck: done minicrm=%s extension=%s", mc.state.value, ext.state.value)
        token = self._recheck_token
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
        self._msg(self.recheck_result, text, color)
