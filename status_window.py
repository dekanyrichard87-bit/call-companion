"""Main window (normal user mode) and the data model behind it.

v0.6.0: the old status window is split in two.
  * MainWindow (this file) is what every user sees: one plain status line ("Minden rendben • Aktív" or a
    problem in plain Hungarian with a short error code and a "Hibajelentés másolása" button) and the persistent
    call history. No double-clicks: a row is selected with one click and dedicated buttons act on it.
  * DevWindow (dev_window.py) holds the technical tools (health details, simulator, MiniCRM re-check, live log);
    it is reachable only when developer mode is switched on.

StatusModel is fed by the controller (main.py) and persisted through history.HistoryStore.
Run `python status_window.py` for a standalone preview with sample data.
"""
from __future__ import annotations

import logging
import os
import platform
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from PySide6.QtCore import QObject, QRect, QSize, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QDesktopServices, QFont, QFontMetrics, QGuiApplication, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPushButton, QStyle, QStyledItemDelegate, QSystemTrayIcon, QVBoxLayout, QWidget,
)

log = logging.getLogger("callcompanion.status")

# ----------------------------------------------------------------- palette --
BASE, MANTLE, SURFACE, SURFACE2, OVERLAY = "#1e1e2e", "#181825", "#313244", "#45475a", "#585b70"
TEXT, SUBTEXT = "#cdd6f4", "#a6adc8"
GREEN, YELLOW, RED, BLUE, MAUVE, LAVENDER, PEACH = (
    "#a6e3a1", "#f9e2af", "#f38ba8", "#89b4fa", "#cba6f7", "#b4befe", "#fab387")
BURGUNDY = "#8c2f45"          # missed calls: deep, quiet red


# ------------------------------------------------------------------- model --
class Health(Enum):
    OK = "ok"
    WARN = "warn"
    ERROR = "error"
    UNKNOWN = "unknown"


HEALTH_COLOR = {Health.OK: GREEN, Health.WARN: YELLOW, Health.ERROR: RED, Health.UNKNOWN: SUBTEXT}

STATUS_HU = {"ringing": "Csörög", "answered": "Folyamatban", "hold": "Tartva", "ended": "Befejezve",
             "missed": "Nem fogadott", "rejected": "Elutasítva", "unanswered": "Nem vették fel",
             "elsewhere": "Máshol fogadták", "transferred": "Átkapcsolva"}
STATUS_COLOR = {"ringing": GREEN, "answered": BLUE, "hold": PEACH, "ended": SUBTEXT, "missed": RED,
                "rejected": YELLOW, "unanswered": SUBTEXT, "elsewhere": SUBTEXT, "transferred": MAUVE}


@dataclass
class HealthItem:
    state: Health = Health.UNKNOWN
    text: str = "Ellenőrzés…"


@dataclass
class CallRecord:
    call_id: str
    number: str
    started: datetime
    status: str = "ringing"          # see STATUS_HU
    name: str | None = None
    lookup: str = "pending"          # pending / found / unknown / failed
    detail: str = ""
    profile_url: str | None = None
    elapsed_ms: int = 0
    direction: str = "in"            # "in" | "out"
    company: str | None = None       # v0.6.0
    duration_s: int = 0              # v0.6.0: talk time of an answered call
    note: str = ""                   # v0.8.0: e.g. "Átkapcsolva: Kovács Péter (102)"


@dataclass
class Summary:
    """The one-line state the normal window shows."""
    state: Health
    title: str
    hint: str
    code: str = ""


class StatusModel(QObject):
    """Everything both windows show. Call from the Qt main thread; each mutation emits `changed` once."""

    changed = Signal()
    recheck_finished = Signal()      # emitted by main.Controller.on_session after every session check
    MAX_CALLS = 300                  # kept in memory / shown; the database keeps more (history.max_rows)
    STARTUP_GRACE_S = 25             # "Indítás…" instead of a warning while the extension is still connecting

    def __init__(self, store=None) -> None:
        super().__init__()
        self.minicrm = HealthItem()
        self.bria = HealthItem()
        self.extension = HealthItem()
        self.api = HealthItem()
        self.bria_api = HealthItem(Health.UNKNOWN, "Még nem volt használva")    # v0.8.0: hold / transfer
        self.store = store
        self.started = time.monotonic()
        self.calls: list[CallRecord] = []
        if store is not None:
            for row in store.load(self.MAX_CALLS):
                fields = {k: row[k] for k in row if k in CallRecord.__dataclass_fields__}
                self.calls.append(CallRecord(**fields))

    # -- health ---------------------------------------------------------
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

    def set_bria_api(self, state: Health, text: str) -> None:
        self._set_health("bria_api", state, text)

    def summary(self) -> Summary:
        """Plain-language state for the normal window: the most important problem first, with a short code the
        user can read out or paste when reporting it."""
        starting = time.monotonic() - self.started < self.STARTUP_GRACE_S
        b, m, e, a = self.bria.state, self.minicrm.state, self.extension.state, self.api.state
        if a is Health.ERROR:
            return Summary(Health.ERROR, "A program egy része nem tudott elindulni",
                           "Indítsa újra a számítógépet. Ha a hiba megmarad, másolja ki a hibajelentést és küldje el "
                           "a rendszergazdának.", "API-PORT")
        if b is Health.ERROR or (b is Health.WARN and not starting):
            return Summary(Health.ERROR if b is Health.ERROR else Health.WARN,
                           "A Bria hívásai most nem láthatók",
                           "Ellenőrizze, hogy fut-e a Bria. Ha fut, lépjen ki a Call Companionből és indítsa újra.",
                           "BRIA-LOG")
        if m is Health.ERROR:
            return Summary(Health.ERROR, "Jelentkezzen be a MiniCRM-be",
                           "Nyissa meg a Chrome-ot és lépjen be a MiniCRM-be. Addig a hívások név nélkül "
                           "jelennek meg.", "MINICRM-LOGIN")
        if e in (Health.WARN, Health.UNKNOWN) and not starting and m is not Health.OK:
            return Summary(Health.WARN, "Nincs kapcsolat a Chrome-mal",
                           "Nyissa meg a Chrome-ot (benne a MiniCRM-mel). Ha nyitva van, ellenőrizze, hogy a "
                           "Call Companion bővítmény be van-e kapcsolva.", "EXT-OFFLINE")
        if m is Health.WARN and not starting:
            return Summary(Health.WARN, "A MiniCRM most nem érhető el",
                           "Ellenőrizze az internetkapcsolatot. A hívások ettől még megjelennek, csak név "
                           "nélkül.", "MINICRM-NET")
        if starting and Health.UNKNOWN in (b, m, e):
            return Summary(Health.UNKNOWN, "Indítás…", "A Call Companion most kapcsolódik a Briához és a "
                                                       "MiniCRM-hez.")
        if self.bria_api.state is Health.ERROR:
            return Summary(Health.WARN, "A Tartás / Átkapcsolás gomb nem működik",
                           "A hívások figyelése rendben van. A Bria nem engedi a vezérlést: a Briában "
                           "engedélyezze az alkalmazások hozzáférését.", "BRIA-API")
        return Summary(Health.OK, "Minden rendben • Aktív", "A Call Companion figyeli a hívásokat.")

    def report_text(self, version: str) -> str:
        """What 'Hibajelentés másolása' puts on the clipboard: short, no caller data."""
        s = self.summary()
        lines = [f"Call Companion {version} – hibajelentés {datetime.now():%Y-%m-%d %H:%M}",
                 f"Állapot: {s.code or 'OK'} – {s.title}",
                 f"Bria napló: {self.bria.state.value} – {self.bria.text}",
                 f"MiniCRM: {self.minicrm.state.value} – {self.minicrm.text}",
                 f"Chrome bővítmény: {self.extension.state.value} – {self.extension.text}",
                 f"Helyi API: {self.api.state.value} – {self.api.text}",
                 f"Bria vezérlés: {self.bria_api.state.value} – {self.bria_api.text}",
                 f"Gép: {platform.node()} • Windows {platform.release()} {platform.version()}"]
        return "\n".join(lines)

    # -- calls ----------------------------------------------------------
    def _find(self, call_id: str) -> CallRecord | None:
        return next((c for c in self.calls if c.call_id == call_id), None)

    def _commit(self, rec: CallRecord | None) -> None:
        if rec is not None and self.store is not None:
            self.store.save(rec)
        self.changed.emit()

    def call_ringing(self, call_id: str, number: str, direction: str = "in") -> None:
        if self._find(call_id):
            return
        rec = CallRecord(call_id, number, datetime.now(), direction=direction)
        self.calls.insert(0, rec)
        del self.calls[self.MAX_CALLS:]
        self._commit(rec)

    def call_resolved(self, call_id: str, name: str | None, profile_url: str | None = None,
                      state: str = "found", detail: str = "", elapsed_ms: int = 0) -> None:
        rec = self._find(call_id)
        if rec:
            rec.name, rec.profile_url = name, profile_url
            rec.lookup, rec.detail, rec.elapsed_ms = state, detail, elapsed_ms
            self._commit(rec)

    def call_company(self, call_id: str, company: str) -> None:
        rec = self._find(call_id)
        if rec and company and rec.company != company:
            rec.company = company
            self._commit(rec)

    def call_answered(self, call_id: str) -> None:
        rec = self._find(call_id)
        if rec:
            rec.status = "answered"
            self._commit(rec)

    def call_hold(self, call_id: str, held: bool) -> None:
        rec = self._find(call_id)
        if rec and rec.status in ("answered", "hold"):
            rec.status = "hold" if held else "answered"
            self._commit(rec)

    def call_note(self, call_id: str, note: str, status: str | None = None) -> None:
        rec = self._find(call_id)
        if rec:
            rec.note = note
            if status:
                rec.status = status
            self._commit(rec)

    def call_ended(self, call_id: str, rejected: bool = False, outcome: str | None = None,
                   duration_s: int = 0) -> None:
        """`outcome` (missed / rejected / unanswered / elsewhere) names how a call that was never answered ended."""
        rec = self._find(call_id)
        if not rec:
            return
        if rec.status == "ringing":
            rec.status = outcome or ("rejected" if rejected else "missed")
        elif rec.status in ("answered", "hold"):
            rec.status = "ended"
        if duration_s:
            rec.duration_s = int(duration_s)
        self._commit(rec)

    def clear_history(self) -> None:
        live = [c for c in self.calls if c.status in ("ringing", "answered", "hold")]
        self.calls = live
        if self.store is not None:
            self.store.clear()
            for rec in live:
                self.store.save(rec)
        self.changed.emit()


# ------------------------------------------------------------ list drawing --
def _fmt_duration(s: int) -> str:
    s = max(0, int(s))
    h, rest = divmod(s, 3600)
    m, sec = divmod(rest, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _when(dt: datetime) -> str:
    today = datetime.now().date()
    if dt.date() == today:
        return dt.strftime("%H:%M")
    if (today - dt.date()).days == 1:
        return "tegnap " + dt.strftime("%H:%M")
    return dt.strftime("%m.%d. %H:%M")


def caller_title(c: CallRecord) -> tuple[str, str]:
    """(text, color) of the first line."""
    if c.name:
        return c.name, TEXT
    if c.lookup == "pending":
        return c.number or "Ismeretlen szám", TEXT
    if c.lookup == "failed":
        return c.number or "Ismeretlen szám", YELLOW
    return "Ismeretlen hívó", SUBTEXT


def _font(px: int, weight=QFont.Weight.Normal) -> QFont:
    f = QFont("Segoe UI")
    f.setPixelSize(px)
    f.setWeight(weight)
    return f


class CallDelegate(QStyledItemDelegate):
    """Two-line call row: name + company tag / direction, number, outcome, duration; time on the right."""
    H = 62

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), self.H)

    def paint(self, p: QPainter, option, index):
        c: CallRecord = index.data(Qt.ItemDataRole.UserRole)
        if c is None:
            return
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = option.rect.adjusted(2, 3, -2, -3)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        missed = c.status == "missed"
        bg = QColor(SURFACE2 if selected else ("#3a3b4f" if hover else SURFACE))
        if missed and not selected:
            bg = QColor("#3a2733" if hover else "#33232d")     # quiet burgundy tint
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(r, 10, 10)
        if missed:
            p.setBrush(QColor(BURGUNDY))
            p.drawRoundedRect(QRect(r.left(), r.top() + 8, 4, r.height() - 16), 2, 2)

        # status disc with the direction arrow
        col = QColor(STATUS_COLOR.get(c.status, TEXT))
        disc = QRect(r.left() + 14, r.center().y() - 15, 30, 30)
        fill = QColor(col)
        fill.setAlpha(45)
        p.setBrush(fill)
        p.drawEllipse(disc)
        p.setPen(col)
        p.setFont(_font(14, QFont.Weight.Bold))
        arrow = "↗" if c.direction == "out" else ("↙" if c.status != "missed" else "✕")
        p.drawText(disc, Qt.AlignmentFlag.AlignCenter, arrow)

        # right column: time
        p.setFont(_font(12))
        p.setPen(QColor(SUBTEXT))
        when = _when(c.started)
        wfm = QFontMetrics(p.font())
        wrect = QRect(r.right() - 12 - wfm.horizontalAdvance(when), r.top() + 11, wfm.horizontalAdvance(when) + 2, 18)
        p.drawText(wrect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, when)

        x0 = disc.right() + 12
        right = wrect.left() - 10
        # line 1: name (bold) + company tag (highlighted)
        title, tcolor = caller_title(c)
        nf = _font(14, QFont.Weight.DemiBold)
        nfm = QFontMetrics(nf)
        company = c.company or ""
        cf = _font(12, QFont.Weight.DemiBold)
        cfm = QFontMetrics(cf)
        avail = right - x0
        tag_w = 0
        if company:
            tag_w = min(cfm.horizontalAdvance(company) + 16, max(60, avail // 2))
        name_txt = nfm.elidedText(title, Qt.TextElideMode.ElideRight, max(40, avail - (tag_w + 8 if tag_w else 0)))
        p.setFont(nf)
        p.setPen(QColor(tcolor))
        y1 = r.top() + 9
        p.drawText(QRect(x0, y1, avail, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name_txt)
        if company:
            tx = x0 + nfm.horizontalAdvance(name_txt) + 8
            tag = QRect(tx, y1 + 1, min(tag_w, right - tx), 18)
            if tag.width() > 24:
                tbg = QColor(LAVENDER)
                tbg.setAlpha(38)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(tbg)
                p.drawRoundedRect(tag, 9, 9)
                p.setFont(cf)
                p.setPen(QColor(LAVENDER))
                p.drawText(tag.adjusted(8, 0, -8, 0), Qt.AlignmentFlag.AlignVCenter,
                           cfm.elidedText(company, Qt.TextElideMode.ElideRight, tag.width() - 16))

        # line 2: number · outcome · duration · note
        parts = []
        if c.name or c.lookup not in ("pending", "failed"):
            parts.append((c.number, SUBTEXT))
        elif c.lookup == "failed":
            parts.append(("Keresés sikertelen", YELLOW))
        else:
            parts.append(("Keresés…", SUBTEXT))
        parts.append((STATUS_HU.get(c.status, c.status), STATUS_COLOR.get(c.status, SUBTEXT)))
        if c.duration_s:
            parts.append((_fmt_duration(c.duration_s), SUBTEXT))
        if c.note:
            parts.append((c.note, MAUVE))
        sf = _font(12)
        sfm = QFontMetrics(sf)
        p.setFont(sf)
        x, y2 = x0, r.top() + 33
        for i, (txt, colr) in enumerate(parts):
            if i:
                p.setPen(QColor(OVERLAY))
                p.drawText(QRect(x, y2, 14, 18), Qt.AlignmentFlag.AlignCenter, "·")
                x += 14
            room = r.right() - 12 - x
            if room < 20:
                break
            t = sfm.elidedText(txt, Qt.TextElideMode.ElideRight, room)
            p.setPen(QColor(colr))
            p.drawText(QRect(x, y2, room, 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, t)
            x += sfm.horizontalAdvance(t)
        p.restore()


# ------------------------------------------------------------------ window --
QSS = f"""
QWidget {{ background: {BASE}; color: {TEXT}; font-family: "Segoe UI"; font-size: 13px; }}
QLabel {{ background: transparent; }}
QFrame#statusCard {{ background: {SURFACE}; border-radius: 14px; }}
QLabel#statusTitle {{ font-size: 18px; font-weight: 700; }}
QLabel#statusHint {{ color: {SUBTEXT}; font-size: 12px; }}
QLabel#code {{ color: {SUBTEXT}; font-size: 11px; font-family: Consolas, "Segoe UI"; }}
QLabel#section {{ color: {SUBTEXT}; font-size: 11px; font-weight: 600; letter-spacing: 1px; }}
QLabel#version {{ color: {OVERLAY}; font-size: 11px; }}
QListWidget {{ background: transparent; border: none; outline: none; }}
QListWidget::item {{ background: transparent; border: none; }}
QLineEdit {{ background: {MANTLE}; border: 1px solid {SURFACE2}; border-radius: 8px; padding: 6px 10px; }}
QLineEdit:focus {{ border-color: {BLUE}; }}
QPushButton {{ background: {SURFACE2}; border: none; border-radius: 8px; padding: 7px 14px;
              font-family: "Segoe UI", "Segoe UI Emoji"; }}
QPushButton:hover {{ background: {OVERLAY}; }}
QPushButton:disabled {{ color: {OVERLAY}; background: {SURFACE}; }}
QPushButton#seg {{ background: transparent; color: {SUBTEXT}; padding: 5px 12px; border-radius: 13px; }}
QPushButton#seg:hover {{ background: {SURFACE}; color: {TEXT}; }}
QPushButton#seg:checked {{ background: {SURFACE2}; color: {TEXT}; font-weight: 600; }}
QPushButton#done {{ background: #2f4a3a; color: {GREEN}; }}
QCheckBox {{ color: {SUBTEXT}; spacing: 8px; }}
QCheckBox::indicator {{ width: 34px; height: 18px; border-radius: 9px; background: {SURFACE2}; }}
QCheckBox::indicator:checked {{ background: {BLUE}; }}
"""


class MainWindow(QWidget):
    """Normal user window: plain status + call history. Signals are wired in main.py."""
    dev_mode_toggled = Signal(bool)
    open_dev_tools = Signal()
    edit_extensions = Signal()          # v0.8.0: "Mellékek…" button

    FILTERS = (("all", "Összes"), ("missed", "Nem fogadott"), ("today", "Mai"))

    def __init__(self, model: StatusModel, version: str = "", dev_mode: bool = False,
                 data_dir: str | None = None) -> None:
        super().__init__()
        self.model = model
        self.version = version
        self._really_close = False
        self._tray: QSystemTrayIcon | None = None
        self._filter = "all"
        self.data_dir = data_dir or os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "CallCompanion")

        self.setWindowTitle("Call Companion")
        self.setMinimumSize(560, 600)
        self.resize(600, 680)
        self.setStyleSheet(QSS)
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(10)

        # ---- status card ----
        card = QFrame(objectName="statusCard")
        cl = QHBoxLayout(card)
        cl.setContentsMargins(16, 14, 16, 14)
        cl.setSpacing(14)
        self.dot = QLabel("●")
        self.dot.setFixedWidth(26)
        self.dot.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        cl.addWidget(self.dot)
        txt = QVBoxLayout()
        txt.setSpacing(3)
        self.st_title = QLabel(objectName="statusTitle")
        self.st_hint = QLabel(objectName="statusHint")
        self.st_hint.setWordWrap(True)
        self.st_code = QLabel(objectName="code")
        txt.addWidget(self.st_title)
        txt.addWidget(self.st_hint)
        txt.addWidget(self.st_code)
        cl.addLayout(txt, 1)
        self.report_btn = QPushButton("📋 Hibajelentés másolása")
        self.report_btn.setToolTip("A hiba leírása a vágólapra kerül (hívóadatok nélkül) – illessze be egy e-mailbe.")
        self.report_btn.clicked.connect(self._copy_report)
        cl.addWidget(self.report_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addWidget(card)

        # ---- history header: title, filter, search ----
        head = QHBoxLayout()
        head.addWidget(self._section("HÍVÁSELŐZMÉNYEK"))
        head.addStretch(1)
        self._seg = QButtonGroup(self)
        self._seg.setExclusive(True)
        for key, label in self.FILTERS:
            b = QPushButton(label, objectName="seg")
            b.setCheckable(True)
            b.setChecked(key == "all")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: self._set_filter(k))
            self._seg.addButton(b)
            head.addWidget(b)
        root.addLayout(head)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Keresés név, cég vagy telefonszám alapján…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _t: self.refresh())
        root.addWidget(self.search)

        # ---- list ----
        self.list = QListWidget()
        self.list.setItemDelegate(CallDelegate(self.list))
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setMouseTracking(True)
        self.list.setUniformItemSizes(True)
        self.list.currentItemChanged.connect(lambda *_: self._update_actions())
        root.addWidget(self.list, 1)
        self.empty = QLabel()
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setStyleSheet(f"color: {SUBTEXT}; padding: 30px;")
        root.addWidget(self.empty, 1)

        # ---- action bar for the selected call (replaces the old double-click) ----
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.sel_label = QLabel()
        self.sel_label.setStyleSheet(f"color: {SUBTEXT}; font-size: 12px;")
        self.btn_crm = QPushButton("🔍 MiniCRM megnyitása")
        self.btn_copy = QPushButton("📋 Szám másolása")
        self.btn_google = QPushButton("🌐 Google keresés")
        for b in (self.btn_crm, self.btn_copy, self.btn_google):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_crm.clicked.connect(self._open_crm)
        self.btn_copy.clicked.connect(self._copy_number)
        self.btn_google.clicked.connect(self._google)
        bar.addWidget(self.btn_crm)
        bar.addWidget(self.btn_copy)
        bar.addWidget(self.btn_google)
        bar.addStretch(1)
        bar.addWidget(self.sel_label)
        root.addLayout(bar)

        # ---- footer ----
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background: {SURFACE};")
        root.addWidget(line)
        foot = QHBoxLayout()
        self.ext_btn = QPushButton("☎ Mellékek…")
        self.ext_btn.setToolTip("Belső mellékek és kollégák – ezekre lehet átkapcsolni a hívást a kártyáról.")
        self.ext_btn.clicked.connect(self.edit_extensions.emit)
        foot.addWidget(self.ext_btn)
        self.dev_btn = QPushButton("🛠 Fejlesztői eszközök")
        self.dev_btn.clicked.connect(self.open_dev_tools.emit)
        foot.addWidget(self.dev_btn)
        foot.addStretch(1)
        self.dev_toggle = QCheckBox("Fejlesztői mód")
        self.dev_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dev_toggle.setChecked(dev_mode)
        self.dev_toggle.toggled.connect(self._dev_toggled)
        foot.addWidget(self.dev_toggle)
        ver = QLabel(f"v{version}" if version else "", objectName="version")
        foot.addWidget(ver)
        root.addLayout(foot)
        self.set_dev_mode(dev_mode)

        self._summary_timer = QTimer(self)        # the start-up grace period ends without a model change
        self._summary_timer.setInterval(5000)
        self._summary_timer.timeout.connect(self._refresh_summary)
        model.changed.connect(self._model_changed)
        self.refresh()

    # ------------------------------------------------------------ helpers --
    @staticmethod
    def _section(text: str) -> QLabel:
        lab = QLabel(text)
        lab.setObjectName("section")
        return lab

    def set_dev_mode(self, on: bool) -> None:
        self.dev_btn.setVisible(on)
        if self.dev_toggle.isChecked() != on:
            self.dev_toggle.blockSignals(True)
            self.dev_toggle.setChecked(on)
            self.dev_toggle.blockSignals(False)

    def _dev_toggled(self, on: bool) -> None:
        self.dev_btn.setVisible(on)
        self.dev_mode_toggled.emit(on)

    def set_tray(self, tray: QSystemTrayIcon) -> None:
        self._tray = tray

    def _placement_screen(self):
        if self._tray is not None:
            g = self._tray.geometry()
            if g.isValid() and not g.isEmpty():
                s = QGuiApplication.screenAt(g.center())
                if s:
                    return s
        return QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()

    def place_near_tray(self, margin: int = 12) -> None:
        area = self._placement_screen().availableGeometry()
        fg = self.frameGeometry()
        self.move(max(area.left(), area.right() - fg.width() + 1 - margin),
                  max(area.top(), area.bottom() - fg.height() + 1 - margin))

    def show_and_raise(self, history: bool = False) -> None:
        was_hidden = not self.isVisible()
        self.refresh()
        if was_hidden:
            self.place_near_tray()
        self.showNormal()
        if was_hidden:
            self.place_near_tray()
        self.raise_()
        self.activateWindow()
        if history:
            self.list.setFocus()
            if self.list.count() and self.list.currentRow() < 0:
                self.list.setCurrentRow(0)

    def prepare_quit(self) -> None:
        self._really_close = True

    def closeEvent(self, event) -> None:
        if self._really_close or not event.spontaneous():
            event.accept()
            return
        event.ignore()
        self.hide()

    def showEvent(self, e) -> None:
        self._summary_timer.start()
        super().showEvent(e)

    def hideEvent(self, e) -> None:
        self._summary_timer.stop()
        super().hideEvent(e)

    # ------------------------------------------------------------ refresh --
    def _model_changed(self) -> None:
        if self.isVisible():
            self.refresh()

    def _refresh_summary(self) -> None:
        s = self.model.summary()
        self.dot.setStyleSheet(f"color: {HEALTH_COLOR[s.state]}; font-size: 22px;")
        self.st_title.setText(s.title)
        self.st_hint.setText(s.hint)
        self.st_code.setText(f"Hibakód: {s.code}" if s.code else "")
        self.st_code.setVisible(bool(s.code))
        self.report_btn.setVisible(s.state in (Health.WARN, Health.ERROR))

    def _visible_calls(self) -> list[CallRecord]:
        q = self.search.text().strip().lower()
        qd = "".join(ch for ch in q if ch.isdigit())
        today = datetime.now().date()
        out = []
        for c in self.model.calls:
            if self._filter == "missed" and c.status != "missed":
                continue
            if self._filter == "today" and c.started.date() != today:
                continue
            if q:
                hay = " ".join(filter(None, (c.name, c.company, c.number, c.note))).lower()
                digits = "".join(ch for ch in c.number if ch.isdigit())
                if q not in hay and not (len(qd) >= 3 and qd[-9:] in digits):
                    continue
            out.append(c)
        return out

    def refresh(self) -> None:
        self._refresh_summary()
        current = self.list.currentItem()
        keep = current.data(Qt.ItemDataRole.UserRole).call_id if current else None
        calls = self._visible_calls()
        self.list.setUpdatesEnabled(False)
        self.list.clear()
        sel = -1
        for i, c in enumerate(calls):
            it = QListWidgetItem()
            it.setData(Qt.ItemDataRole.UserRole, c)
            tip = [c.number]
            if c.elapsed_ms:
                tip.append(f"MiniCRM keresés: {c.elapsed_ms} ms")
            it.setToolTip("\n".join(tip))
            self.list.addItem(it)
            if c.call_id == keep:
                sel = i
        if sel >= 0:
            self.list.setCurrentRow(sel)
        self.list.setUpdatesEnabled(True)
        has = bool(calls)
        self.list.setVisible(has)
        self.empty.setVisible(not has)
        if not has:
            self.empty.setText("Nincs a szűrésnek megfelelő hívás." if self.model.calls
                               else "Még nem volt hívás.\nA bejövő és kimenő hívások itt jelennek meg.")
        self._update_actions()

    def _set_filter(self, key: str) -> None:
        self._filter = key
        self.refresh()

    # ------------------------------------------------------------ actions --
    def _selected(self) -> CallRecord | None:
        it = self.list.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def _update_actions(self) -> None:
        c = self._selected()
        self.btn_crm.setEnabled(bool(c and c.profile_url))
        self.btn_copy.setEnabled(bool(c and c.number))
        self.btn_google.setEnabled(bool(c and c.number))
        self.btn_crm.setToolTip("" if (c and c.profile_url) else "Ez a hívó nincs meg a MiniCRM-ben.")
        self.sel_label.setText("" if c else "Válasszon ki egy hívást a listából.")

    def _open_crm(self) -> None:
        c = self._selected()
        if c and c.profile_url:
            QDesktopServices.openUrl(QUrl(c.profile_url))

    def _google(self) -> None:
        c = self._selected()
        if c and c.number:
            from urllib.parse import quote
            QDesktopServices.openUrl(QUrl("https://www.google.com/search?q=" + quote(c.number)))

    def _copy_number(self) -> None:
        c = self._selected()
        if c and c.number:
            QApplication.clipboard().setText(c.number)
            flash(self.btn_copy, "✓ Másolva")

    def _copy_report(self) -> None:
        QApplication.clipboard().setText(self.model.report_text(self.version))
        log.info("problem report copied to the clipboard (%s)", self.model.summary().code)
        flash(self.report_btn, "✓ A vágólapra másolva")


StatusWindow = MainWindow       # old name (0.3 - 0.5)


def flash(button: QPushButton, text: str, ms: int = 1400) -> None:
    """Micro-interaction: the button briefly turns green with a confirmation text, then restores itself."""
    if button.property("_flash_orig") is None:
        button.setProperty("_flash_orig", button.text())
        button.setProperty("_flash_name", button.objectName())
    button.setText(text)
    button.setObjectName("done")
    button.style().unpolish(button)
    button.style().polish(button)
    token = (button.property("_flash_token") or 0) + 1
    button.setProperty("_flash_token", token)

    def restore():
        try:
            if button.property("_flash_token") != token:
                return
            button.setText(button.property("_flash_orig"))
            button.setObjectName(button.property("_flash_name") or "")
            button.setProperty("_flash_orig", None)
            button.style().unpolish(button)
            button.style().polish(button)
        except RuntimeError:          # the button is gone
            pass
    QTimer.singleShot(ms, restore)


# -------------------------------------------------------------------- tray --
def attach_to_tray(tray: QSystemTrayIcon, window: MainWindow) -> None:
    """One click on the tray icon opens the main window (no double-click needed); right click = menu."""
    window.set_tray(tray)

    def on_activated(reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            window.show_and_raise()
    tray.activated.connect(on_activated)


# ---------------------------------------------------------- standalone demo --
def _demo() -> int:
    app = QApplication(sys.argv)
    model = StatusModel()
    win = MainWindow(model, "demo")
    win.prepare_quit()
    model.started -= 60
    for s in ("set_minicrm", "set_bria", "set_extension", "set_api"):
        getattr(model, s)(Health.OK, "ok")
    model.call_ringing("a", "+36301234567")
    model.call_resolved("a", "Minta Anna", "https://r3.minicrm.hu/60171/#AddressBook/1")
    model.call_company("a", "Feszültség Kft.")
    model.call_answered("a")
    model.call_ended("a", duration_s=135)
    model.call_ringing("b", "+4915123456789")
    model.call_resolved("b", None, state="unknown")
    model.call_ended("b")
    model.call_ringing("c", "+36201112233", "out")
    win.show_and_raise()
    app.setQuitOnLastWindowClosed(True)
    return app.exec()


if __name__ == "__main__":
    sys.exit(_demo())
