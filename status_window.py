"""Main window (normal user mode) and the data model behind it.

v0.6.0: the old status window is split in two.
  * MainWindow (this file) is what every user sees: one plain status line ("Minden rendben • Aktív" or a
    problem in plain Hungarian with a short error code and a "Hibajelentés másolása" button) and the persistent
    call history. No double-clicks. v0.9.0: day groups (one click folds a day), type + date filters over the whole
    stored history, row actions inside each row, ⚙ opens the settings dialog (settings_dialog.py).
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
from datetime import date, datetime, timedelta
from enum import Enum

from PySide6.QtCore import QEvent, QObject, QRect, QSize, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import (QColor, QCursor, QDesktopServices, QFont, QFontMetrics, QGuiApplication, QKeySequence,
                           QPainter, QShortcut)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPushButton, QStyle, QStyledItemDelegate, QSystemTrayIcon, QToolTip, QVBoxLayout,
    QWidget,
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
    STARTUP_GRACE_S = 60             # "Indítás…" instead of a warning while the extension is still connecting
                                     # (v0.9.0: was 25 s, shorter than the extension's retry / alarm)

    def __init__(self, store=None) -> None:
        super().__init__()
        self.minicrm = HealthItem()
        self.bria = HealthItem()
        self.extension = HealthItem()
        self.api = HealthItem()
        self.bria_api = HealthItem(Health.UNKNOWN, "Még nem volt használva")    # v0.8.0: hold / transfer
        self.store = store
        self.ext_version: str | None = None      # v0.9.0: None = no push yet, "" = an extension older than 0.9.0
        self.ext_expected = ""                   # version shipped with the app
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

    def set_extension_version(self, version: str, expected: str) -> None:
        if (version, expected) != (self.ext_version, self.ext_expected):
            self.ext_version, self.ext_expected = version, expected
            self.changed.emit()

    def extension_outdated(self) -> bool:
        """The extension that pushed is older than the one shipped with the app (EXT-OLD)."""
        if self.ext_version is None or not self.ext_expected:
            return False
        return _version_tuple(self.ext_version) < _version_tuple(self.ext_expected)

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
        if self.extension_outdated():
            return Summary(Health.WARN, "Frissítse a Chrome bővítményt",
                           "A Call Companion újabb bővítménnyel érkezett. Nyissa meg a Chrome-ban a "
                           "chrome://extensions oldalt, és a Call Companion bővítményen kattintson a ↻ ikonra.",
                           "EXT-OLD")
        if self.bria_api.state is Health.ERROR:
            return Summary(Health.WARN, "A kártya hívásgombjai nem működnek",
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
                 f"Bővítmény verzió: {self.ext_version or ('régi (0.9 előtti)' if self.ext_version == '' else '?')}"
                 f" (a programhoz tartozó: {self.ext_expected or '?'})",
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

    def history_calls(self, since: datetime | None = None, until: datetime | None = None) -> list[CallRecord]:
        """v0.9.0: calls in [since, until) from the whole stored history (not only the MAX_CALLS in memory),
        newest first. Records that are in memory are returned as those objects (they carry live changes).
        Without a working store: the in-memory list, filtered."""
        def inside(c):
            return (since is None or c.started >= since) and (until is None or c.started < until)
        mem = {c.call_id: c for c in self.calls if inside(c)}
        if self.store is None or not getattr(self.store, "ok", False):
            return [c for c in self.calls if inside(c)]
        out = []
        for row in self.store.query(since, until):
            rec = mem.pop(row["call_id"], None)
            if rec is None:
                rec = CallRecord(**{k: row[k] for k in row if k in CallRecord.__dataclass_fields__})
            out.append(rec)
        out.extend(mem.values())                   # not yet in the store (save failed): keep them visible
        out.sort(key=lambda c: c.started, reverse=True)
        return out

    def clear_history(self) -> None:
        live = [c for c in self.calls if c.status in ("ringing", "answered", "hold")]
        self.calls = live
        if self.store is not None:
            self.store.clear()
            for rec in live:
                self.store.save(rec)
        self.changed.emit()


def _version_tuple(v: str) -> tuple:
    """'0.9.0' -> (0, 9, 0); missing / bad parts count as 0 ("" -> (0,), older than anything)."""
    out = []
    for part in str(v or "").split("-")[0].split("."):
        out.append(int(part) if part.isdigit() else 0)
    return tuple(out) or (0,)


# ------------------------------------------------------------ list drawing --
def _fmt_duration(s: int) -> str:
    s = max(0, int(s))
    h, rest = divmod(s, 3600)
    m, sec = divmod(rest, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


WEEKDAYS_HU = ("hétfő", "kedd", "szerda", "csütörtök", "péntek", "szombat", "vasárnap")
MONTHS_HU = ("január", "február", "március", "április", "május", "június", "július", "augusztus", "szeptember",
             "október", "november", "december")


def day_label(d: date, today: date | None = None) -> str:
    """v0.9.0 day header: Ma, Tegnap, "Hétfő, november 10." (this and last week), "2026. október 28., kedd"."""
    today = today or date.today()
    if d == today:
        return "Ma"
    if d == today - timedelta(days=1):
        return "Tegnap"
    wd = WEEKDAYS_HU[d.weekday()]
    if d >= today - timedelta(days=today.weekday() + 7):
        return f"{wd.capitalize()}, {MONTHS_HU[d.month - 1]} {d.day}."
    return f"{d.year}. {MONTHS_HU[d.month - 1]} {d.day}., {wd}"


def date_range(key: str, today: date | None = None) -> tuple[datetime | None, datetime | None]:
    """[since, until) of a date filter key: any | today | yesterday | week | lastweek (weeks: Monday-Sunday)."""
    today = today or date.today()
    start = datetime.combine(today, datetime.min.time())
    monday = start - timedelta(days=today.weekday())
    return {"today": (start, start + timedelta(days=1)),
            "yesterday": (start - timedelta(days=1), start),
            "week": (monday, monday + timedelta(days=7)),
            "lastweek": (monday - timedelta(days=7), monday)}.get(key, (None, None))


def caller_title(c: CallRecord) -> tuple[str, str]:
    """(text, color) of the first line."""
    if c.name:
        return c.name, TEXT
    if c.lookup == "pending":
        return c.number or "Ismeretlen szám", TEXT
    if c.lookup == "failed":
        return c.number or "Ismeretlen szám", YELLOW
    return "Ismeretlen hívó", SUBTEXT


def _font(px: int, weight=QFont.Weight.Normal, emoji: bool = False) -> QFont:
    f = QFont("Segoe UI")
    if emoji:
        f.setFamilies(["Segoe UI Emoji", "Segoe UI"])
    f.setPixelSize(px)
    f.setWeight(weight)
    return f


@dataclass
class DayHeader:
    """A day separator row in the call list (not selectable; one click folds / unfolds the day)."""
    day: date
    label: str
    count: int
    missed: int
    expanded: bool


ROLE = Qt.ItemDataRole.UserRole
ACTIONS = (("crm", "🔍", "MiniCRM megnyitása"), ("copy", "📋", "Szám másolása"), ("google", "🌐", "Google keresés"))


class CallDelegate(QStyledItemDelegate):
    """v0.9.0 call list painter: day headers and two-line call rows. The right column of a row is reserved and
    never shrinks: the time on line 1, the three row actions (🔍 📋 🌐) on line 2 - faint at rest, full colour on
    row hover, a soft disc under the hovered one. Clicks on an action are handled here (editorEvent), so a row
    needs no widgets and no selection to act."""
    H, HH = 62, 36                    # call row / day header height
    BTN_W, BTN_H, BTN_GAP = 26, 22, 4
    NARROW = 480                      # below this row width the company tag moves to line 2
    action = Signal(object, str)      # CallRecord, "crm" | "copy" | "google"

    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self.hover: tuple[int, str | None] | None = None      # (row, action under the mouse)
        self.copied: dict[str, float] = {}                     # call_id -> monotonic time until the ✓ shows

    # ---- geometry ------------------------------------------------------------------------------------------------
    def sizeHint(self, option, index):
        return QSize(0, self.HH if isinstance(index.data(ROLE), DayHeader) else self.H)

    def _right_col_w(self) -> int:
        return 3 * self.BTN_W + 2 * self.BTN_GAP

    def action_rects(self, r: QRect, c: CallRecord) -> dict:
        """kind -> QRect of the row actions (MiniCRM only when the caller is in MiniCRM; its slot stays empty)."""
        x = r.right() - 10 - self._right_col_w()
        y = r.top() + 31
        out = {}
        for i, (kind, _g, _t) in enumerate(ACTIONS):
            if kind == "crm" and not c.profile_url:
                continue
            if kind in ("copy", "google") and not c.number:
                continue
            out[kind] = QRect(x + i * (self.BTN_W + self.BTN_GAP), y, self.BTN_W, self.BTN_H)
        return out

    def hit(self, option, index, pos) -> str | None:
        c = index.data(ROLE)
        if not isinstance(c, CallRecord):
            return None
        r = option.rect.adjusted(2, 3, -2, -3)
        return next((k for k, q in self.action_rects(r, c).items() if q.contains(pos)), None)

    # ---- events --------------------------------------------------------------------------------------------------
    def editorEvent(self, event, model, option, index):
        et = event.type()
        if et not in (QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
                      QEvent.Type.MouseButtonDblClick):
            return False
        kind = self.hit(option, index, event.position().toPoint())
        h = (index.row(), kind)
        if h != self.hover:
            self.hover = h
            self.view.viewport().update()
        if kind is None or event.button() not in (Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton):
            return False
        if et == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            self.action.emit(index.data(ROLE), kind)
        return et != QEvent.Type.MouseMove        # a click on an action never selects the row

    def helpEvent(self, event, view, option, index):
        kind = self.hit(option, index, event.pos())
        if kind:
            QToolTip.showText(event.globalPos(), next(t for k, _g, t in ACTIONS if k == kind), view)
            return True
        return super().helpEvent(event, view, option, index)

    def flash_copied(self, call_id: str, ms: int = 1300):
        self.copied[call_id] = time.monotonic() + ms / 1000
        self.view.viewport().update()
        QTimer.singleShot(ms + 30, self.view.viewport().update)

    # ---- painting ------------------------------------------------------------------------------------------------
    def paint(self, p: QPainter, option, index):
        data = index.data(ROLE)
        if isinstance(data, DayHeader):
            self._paint_header(p, option, data)
        elif isinstance(data, CallRecord):
            self._paint_call(p, option, index, data)

    def _paint_header(self, p: QPainter, option, h: DayHeader):
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = option.rect.adjusted(4, 8, -4, -2)
        if option.state & QStyle.StateFlag.State_MouseOver:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(SURFACE))
            p.drawRoundedRect(r, 8, 8)
        x = r.left() + 8
        p.setFont(_font(12, QFont.Weight.DemiBold))
        p.setPen(QColor(SUBTEXT))
        p.drawText(QRect(x, r.top(), 14, r.height()), Qt.AlignmentFlag.AlignCenter, "▾" if h.expanded else "▸")
        x += 18
        parts = [(h.label, TEXT, True), (f"{h.count} hívás", SUBTEXT, False)]
        if h.missed:
            parts.append((f"{h.missed} nem fogadott", "#e0607e", False))
        for i, (txt, col, bold) in enumerate(parts):
            if i:
                p.setPen(QColor(OVERLAY))
                p.setFont(_font(12))
                p.drawText(QRect(x, r.top(), 16, r.height()), Qt.AlignmentFlag.AlignCenter, "·")
                x += 16
            f = _font(12, QFont.Weight.DemiBold if bold else QFont.Weight.Normal)
            p.setFont(f)
            p.setPen(QColor(col))
            fm = QFontMetrics(f)
            t = fm.elidedText(txt, Qt.TextElideMode.ElideRight, max(0, r.right() - 8 - x))
            p.drawText(QRect(x, r.top(), r.right() - x, r.height()), Qt.AlignmentFlag.AlignVCenter, t)
            x += fm.horizontalAdvance(t)
        p.restore()

    def _paint_call(self, p: QPainter, option, index, c: CallRecord):
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

        # reserved right column: time (line 1) and the row actions (line 2)
        col_w = self._right_col_w()
        col_left = r.right() - 10 - col_w
        p.setFont(_font(12))
        p.setPen(QColor(SUBTEXT))
        p.drawText(QRect(col_left, r.top() + 9, col_w, 20), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   c.started.strftime("%H:%M"))
        self._paint_actions(p, r, c, index.row(), hover)

        x0 = disc.right() + 12
        right = col_left - 10
        avail = max(0, right - x0)
        narrow = r.width() < self.NARROW
        # line 1: name (bold) + company tag; the tag shrinks before the name does
        title, tcolor = caller_title(c)
        nf = _font(14, QFont.Weight.DemiBold)
        nfm = QFontMetrics(nf)
        cf = _font(12, QFont.Weight.DemiBold)
        cfm = QFontMetrics(cf)
        company = c.company or ""
        tag_w = 0
        if company and not narrow:
            natural = cfm.horizontalAdvance(company) + 16
            tag_w = min(natural, avail - nfm.horizontalAdvance(title) - 8)
            if tag_w < 56:                                     # name too long: the tag keeps a small minimum
                tag_w = min(natural, 56) if avail - 64 >= 90 else 0
        name_txt = nfm.elidedText(title, Qt.TextElideMode.ElideRight, max(30, avail - (tag_w + 8 if tag_w else 0)))
        p.setFont(nf)
        p.setPen(QColor(tcolor))
        y1 = r.top() + 9
        p.drawText(QRect(x0, y1, avail, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name_txt)
        if tag_w:
            self._paint_tag(p, QRect(x0 + nfm.horizontalAdvance(name_txt) + 8, y1 + 1, tag_w, 18), company, cf, cfm)

        # line 2: [company tag when narrow] number · outcome · duration · note
        x, y2 = x0, r.top() + 33
        if company and narrow:
            tw = min(cfm.horizontalAdvance(company) + 16, max(0, avail // 2))
            if tw >= 40:
                self._paint_tag(p, QRect(x, y2, tw, 18), company, cf, cfm)
                x += tw + 8
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
        for i, (txt, colr) in enumerate(parts):
            if i:
                if right - x < 34:
                    break
                p.setPen(QColor(OVERLAY))
                p.drawText(QRect(x, y2, 14, 18), Qt.AlignmentFlag.AlignCenter, "·")
                x += 14
            room = right - x
            if room < 20:
                break
            t = sfm.elidedText(txt, Qt.TextElideMode.ElideRight, room)
            p.setPen(QColor(colr))
            p.drawText(QRect(x, y2, room, 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, t)
            x += sfm.horizontalAdvance(t)
        p.restore()

    @staticmethod
    def _paint_tag(p: QPainter, tag: QRect, company: str, cf: QFont, cfm: QFontMetrics):
        if tag.width() <= 24:
            return
        tbg = QColor(LAVENDER)
        tbg.setAlpha(38)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(tbg)
        p.drawRoundedRect(tag, 9, 9)
        p.setFont(cf)
        p.setPen(QColor(LAVENDER))
        p.drawText(tag.adjusted(8, 0, -8, 0), Qt.AlignmentFlag.AlignVCenter,
                   cfm.elidedText(company, Qt.TextElideMode.ElideRight, tag.width() - 16))

    def _paint_actions(self, p: QPainter, r: QRect, c: CallRecord, row: int, row_hover: bool):
        hovered = self.hover[1] if self.hover and self.hover[0] == row else None
        copied = self.copied.get(c.call_id, 0) > time.monotonic()
        for kind, q in self.action_rects(r, c).items():
            glyph = next(g for k, g, _t in ACTIONS if k == kind)
            if kind == "copy" and copied:
                p.setOpacity(1.0)
                p.setPen(Qt.PenStyle.NoPen)
                ok = QColor(GREEN)
                ok.setAlpha(50)
                p.setBrush(ok)
                p.drawRoundedRect(q, 7, 7)
                p.setFont(_font(13, QFont.Weight.Bold))
                p.setPen(QColor(GREEN))
                p.drawText(q, Qt.AlignmentFlag.AlignCenter, "✓")
                continue
            if kind == hovered:
                p.setOpacity(1.0)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(255, 255, 255, 34))
                p.drawRoundedRect(q, 7, 7)
            p.setOpacity(1.0 if kind == hovered else (0.85 if row_hover else 0.35))
            p.setFont(_font(12, emoji=True))
            p.setPen(QColor(TEXT))
            p.drawText(q, Qt.AlignmentFlag.AlignCenter, glyph)
        p.setOpacity(1.0)


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
QPushButton#gear {{ background: transparent; color: {SUBTEXT}; font-size: 18px; padding: 2px 8px; border-radius: 8px; }}
QPushButton#gear:hover {{ background: {SURFACE2}; color: {TEXT}; }}
QPushButton#done {{ background: #2f4a3a; color: {GREEN}; }}
QCheckBox {{ color: {SUBTEXT}; spacing: 8px; }}
QCheckBox::indicator {{ width: 34px; height: 18px; border-radius: 9px; background: {SURFACE2}; }}
QCheckBox::indicator:checked {{ background: {BLUE}; }}
"""


class MainWindow(QWidget):
    """Normal user window: plain status + call history. Signals are wired in main.py."""
    open_settings = Signal()            # v0.9.0: ⚙ gear (Beállítások: Mellékek, Fejlesztői mód, Névjegy)

    TYPE_FILTERS = (("all", "Összes"), ("missed", "Nem fogadott"))
    DATE_FILTERS = (("any", "Bármikor"), ("today", "Mai"), ("yesterday", "Tegnap"), ("week", "A héten"),
                    ("lastweek", "Múlt héten"))

    def __init__(self, model: StatusModel, version: str = "", dev_mode: bool = False,
                 data_dir: str | None = None) -> None:
        super().__init__()
        self.model = model
        self.version = version
        self._really_close = False
        self._tray: QSystemTrayIcon | None = None
        self._type, self._date = "all", "any"
        self._open: dict[date, bool] = {}       # user's fold state per day (kept while the app runs)
        self._open_f: dict[date, bool] = {}     # the same while a filter / search is active (reset when it changes)
        self.data_dir = data_dir or os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "CallCompanion")

        self.setWindowTitle("Call Companion")
        self.setMinimumSize(420, 560)
        self.resize(600, 680)
        self.setStyleSheet(QSS)
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(10)

        # ---- status card (⚙ top right) ----
        card = QFrame(objectName="statusCard")
        cl = QHBoxLayout(card)
        cl.setContentsMargins(16, 14, 10, 14)
        cl.setSpacing(14)
        self.dot = QLabel("●")
        self.dot.setFixedWidth(26)
        self.dot.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        cl.addWidget(self.dot)
        txt = QVBoxLayout()
        txt.setSpacing(3)
        self.st_title = QLabel(objectName="statusTitle")
        self.st_title.setWordWrap(True)
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
        self.gear = QPushButton("⚙", objectName="gear")
        self.gear.setToolTip("Beállítások")
        self.gear.setCursor(Qt.CursorShape.PointingHandCursor)
        self.gear.clicked.connect(self.open_settings.emit)
        cl.addWidget(self.gear, 0, Qt.AlignmentFlag.AlignTop)
        root.addWidget(card)

        # ---- history header: title + type filter, date filter, search ----
        head = QHBoxLayout()
        head.addWidget(self._section("HÍVÁSELŐZMÉNYEK"))
        head.addStretch(1)
        self._type_btns = self._pills(head, self.TYPE_FILTERS, "all", self._set_type)
        root.addLayout(head)
        drow = QHBoxLayout()
        drow.setSpacing(2)
        self._date_btns = self._pills(drow, self.DATE_FILTERS, "any", self._set_date)
        drow.addStretch(1)
        root.addLayout(drow)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Keresés név, cég vagy telefonszám alapján…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _t: self._filters_changed())
        root.addWidget(self.search)

        # ---- list: day headers + call rows (actions inside the rows) ----
        self.list = QListWidget()
        self.delegate = CallDelegate(self.list)
        self.delegate.action.connect(self._row_action)
        self.list.setItemDelegate(self.delegate)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setMouseTracking(True)
        self.list.itemClicked.connect(self._item_clicked)
        self.list.viewport().installEventFilter(self)
        root.addWidget(self.list, 1)
        self.empty = QLabel()
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        self.empty.setStyleSheet(f"color: {SUBTEXT}; padding: 30px;")
        root.addWidget(self.empty, 1)
        for key in ("Return", "Enter"):                    # Enter = MiniCRM, Ctrl+C = copy (keyboard use)
            sc = QShortcut(QKeySequence(key), self.list)
            sc.setContext(Qt.ShortcutContext.WidgetShortcut)
            sc.activated.connect(lambda: self._key_action("crm"))
        sc = QShortcut(QKeySequence(QKeySequence.StandardKey.Copy), self.list)
        sc.setContext(Qt.ShortcutContext.WidgetShortcut)
        sc.activated.connect(lambda: self._key_action("copy"))

        # ---- footer: only the version (settings moved behind ⚙) ----
        foot = QHBoxLayout()
        foot.addStretch(1)
        foot.addWidget(QLabel(f"v{version}" if version else "", objectName="version"))
        root.addLayout(foot)

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

    def _pills(self, layout, items, current, slot) -> dict:
        group = QButtonGroup(self)
        group.setExclusive(True)
        btns = {}
        for key, label in items:
            b = QPushButton(label, objectName="seg")
            b.setCheckable(True)
            b.setChecked(key == current)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: slot(k))
            group.addButton(b)
            layout.addWidget(b)
            btns[key] = b
        return btns

    def set_dev_mode(self, on: bool) -> None:
        """Kept for main.py: developer mode now lives in the settings dialog."""

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
            if self.list.currentRow() < 0:
                for i in range(self.list.count()):
                    if isinstance(self.list.item(i).data(ROLE), CallRecord):
                        self.list.setCurrentRow(i)
                        break

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

    def eventFilter(self, obj, ev) -> bool:
        if obj is self.list.viewport() and ev.type() == QEvent.Type.Leave and self.delegate.hover is not None:
            self.delegate.hover = None
            self.list.viewport().update()
        return False

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

    def _filtering(self) -> bool:
        return self._type != "all" or self._date != "any" or bool(self.search.text().strip())

    def _visible_calls(self) -> list[CallRecord]:
        q = self.search.text().strip().lower()
        qd = "".join(ch for ch in q if ch.isdigit())
        since, until = date_range(self._date)
        # the date filter and the search look at the whole stored history; the plain list at the newest calls
        source = self.model.history_calls(since, until) if (q or self._date != "any") else self.model.calls
        out = []
        for c in source:
            if self._type == "missed" and c.status != "missed":
                continue
            if q:
                hay = " ".join(filter(None, (c.name, c.company, c.number, c.note))).lower()
                digits = "".join(ch for ch in c.number if ch.isdigit())
                if q not in hay and not (len(qd) >= 3 and qd[-9:] in digits):
                    continue
            out.append(c)
        return out

    def _expanded(self, d: date, today: date) -> bool:
        if self._filtering():
            return self._open_f.get(d, True)            # a search / filter opens every group that has hits
        return self._open.get(d, d >= today - timedelta(days=1))   # default: Ma + Tegnap open, older folded

    def refresh(self) -> None:
        self._refresh_summary()
        current = self.list.currentItem()
        cur = current.data(ROLE) if current else None
        keep = cur.call_id if isinstance(cur, CallRecord) else None
        top = self.list.verticalScrollBar().value()
        calls = self._visible_calls()
        today = date.today()
        groups: dict[date, list[CallRecord]] = {}
        for c in calls:
            groups.setdefault(c.started.date(), []).append(c)
        self.list.setUpdatesEnabled(False)
        self.list.clear()
        sel = -1
        for d, items in groups.items():
            exp = self._expanded(d, today)
            hi = QListWidgetItem()
            hi.setData(ROLE, DayHeader(d, day_label(d, today), len(items),
                                       sum(1 for c in items if c.status == "missed"), exp))
            hi.setFlags(Qt.ItemFlag.ItemIsEnabled)
            hi.setToolTip("Kattintás: " + ("összecsukás" if exp else "kinyitás"))
            self.list.addItem(hi)
            if not exp:
                continue
            for c in items:
                it = QListWidgetItem()
                it.setData(ROLE, c)
                if c.elapsed_ms:
                    it.setToolTip(f"MiniCRM keresés: {c.elapsed_ms} ms")
                self.list.addItem(it)
                if c.call_id == keep:
                    sel = self.list.count() - 1
        if sel >= 0:
            self.list.setCurrentRow(sel)
        self.list.verticalScrollBar().setValue(top)
        self.list.setUpdatesEnabled(True)
        has = bool(calls)
        self.list.setVisible(has)
        self.empty.setVisible(not has)
        if not has:
            self.empty.setText(self._empty_text())

    def _empty_text(self) -> str:
        if not self.model.calls and not self._filtering():
            return "Még nem volt hívás.\nA bejövő és kimenő hívások itt jelennek meg."
        what = "nem fogadott hívás" if self._type == "missed" else "hívás"
        q = self.search.text().strip()
        if q:
            when = {"any": "", "today": " ma", "yesterday": " tegnap", "week": " ezen a héten",
                    "lastweek": " a múlt héten"}[self._date]
            return f"Nincs{when} olyan {what}, amely illik erre: „{q}”."
        return {"any": f"Nincs {what} az előzményekben.", "today": f"Ma még nem volt {what}.",
                "yesterday": f"Tegnap nem volt {what}.", "week": f"Ezen a héten még nem volt {what}.",
                "lastweek": f"A múlt héten nem volt {what}."}[self._date]

    def _filters_changed(self) -> None:
        self._open_f.clear()
        self.refresh()

    def _set_type(self, key: str) -> None:
        self._type = key
        self._filters_changed()

    def _set_date(self, key: str) -> None:
        self._date = key
        self._filters_changed()

    def _item_clicked(self, item) -> None:
        h = item.data(ROLE)
        if isinstance(h, DayHeader):                     # one click folds / unfolds the day
            (self._open_f if self._filtering() else self._open)[h.day] = not h.expanded
            self.refresh()

    # ------------------------------------------------------------ actions --
    def _row_action(self, c: CallRecord, kind: str) -> None:
        if not isinstance(c, CallRecord):
            return
        if kind == "crm" and c.profile_url:
            QDesktopServices.openUrl(QUrl(c.profile_url))
        elif kind == "google" and c.number:
            from urllib.parse import quote
            QDesktopServices.openUrl(QUrl("https://www.google.com/search?q=" + quote(c.number)))
        elif kind == "copy" and c.number:
            QApplication.clipboard().setText(c.number)
            self.delegate.flash_copied(c.call_id)

    def _key_action(self, kind: str) -> None:
        it = self.list.currentItem()
        c = it.data(ROLE) if it else None
        if isinstance(c, CallRecord):
            self._row_action(c, kind)

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
    """Preview with sample data (no Bria, no MiniCRM, nothing saved): python status_window.py
    Several days of calls (day groups, filters), a narrow-window check, and ⚙ opens the settings dialog."""
    import tempfile
    app = QApplication(sys.argv)
    model = StatusModel()
    model.started -= 60
    for s in ("set_minicrm", "set_bria", "set_extension", "set_api"):
        getattr(model, s)(Health.OK, "ok")
    samples = [("Minta Anna", "Feszültség Kft.", "+36301234567", "ended", 135, "in"),
               (None, None, "+4915123456789", "missed", 0, "in"),
               ("Kovács Péter", "Nagyon Hosszú Nevű Építőipari és Kereskedelmi Kft.", "+36201112233", "ended", 62,
                "out"),
               ("Nagy Éva", None, "+36705554433", "missed", 0, "in"),
               ("Szabó Gábor", "Minta Bt.", "+36309998877", "rejected", 0, "in")]
    now = datetime.now()
    n = 0
    for days in (0, 0, 1, 1, 2, 4, 9, 16, 30):
        for i, (name, company, number, status, dur, direction) in enumerate(samples[: 2 + days % 4]):
            n += 1
            rec = CallRecord(f"d{n}", number, now - timedelta(days=days, minutes=37 * i + 5), status=status,
                             name=name, lookup="found" if name else "unknown", direction=direction,
                             company=company, duration_s=dur,
                             profile_url="https://r3.minicrm.hu/" if name else None)
            model.calls.append(rec)
    model.calls.sort(key=lambda c: c.started, reverse=True)
    win = MainWindow(model, "demo")
    win.prepare_quit()

    def open_settings():
        from settings import Settings
        from settings_dialog import SettingsDialog
        st = Settings(os.path.join(tempfile.mkdtemp(), "settings.json"))
        SettingsDialog(st, "demo", update_state=lambda: "Up to date (demo)", check_updates=lambda: None,
                       set_dev_mode=lambda on: None, parent=win).exec()
    win.open_settings.connect(open_settings)
    win.show_and_raise()
    app.setQuitOnLastWindowClosed(True)
    return app.exec()


if __name__ == "__main__":
    sys.exit(_demo())
