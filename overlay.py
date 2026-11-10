"""Frameless, always-on-top, dark glass-style call card (bottom-right corner by default).

Card text, top to bottom (see CallOverlay._lines):
    line 1  big, bold         caller's NAME (while the lookup runs: the number)
    line 2  medium            COMPANY name (hidden when there is none; also the "Lookup failed" / notice text)
    line 3  small, muted      the PHONE NUMBER (while the lookup runs: "Looking up..."; after a failed lookup: the error)
An empty line 2 or 3 is hidden, so without a company the number sits directly below the name.

Card states (the status line in the header says which):
    ● INCOMING CALL / ● OUTGOING CALL   ringing / dialling
    ● IN CALL  mm:ss                    answered: stays open with a live duration counter until the call ends
    ● MISSED CALL [×N] · hh:mm          stays until the user closes it (✕) or uses MiniCRM / Google

Several cards can be on screen at once. They are stacked at the anchor: the live call (or a notice) nearest to it,
the missed-call cards beyond it, the newest missed call nearest to the live call.

Placement (v0.5.1, config "ui" block, read from config.load() on first use):
    overlay_position   bottom_right | bottom_left | top_right | top_left   (corner of the usable screen area)
    offset_x/offset_y  px added to the corner position (default offset_x = -380: clears Bria's incoming-call toast)
    dock_to_bria       true = while Bria's toast is on screen, the cards sit directly left of it (bottoms aligned);
                       found with one EnumWindows pass (visible + topmost + toast-sized + Bria process),
                       re-checked every 300 ms only while a live call card is visible. Off by default.
"""
import ctypes
import itertools
import logging
import os
import re
import sys
import time
from ctypes import wintypes
from urllib.parse import quote

from PySide6.QtCore import QPropertyAnimation, QRect, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from shiboken6 import isValid   # ships with PySide6: False once the C++ side of a widget is deleted

import config
from crm import CallerInfo

log = logging.getLogger("overlay")

UNKNOWN_CALLER = "Ismeretlen hívó"

QSS = """
QLabel { color:#cdd6f4; background:transparent; font-family:'Segoe UI'; }
#status { font-size:11px; font-weight:600; }
#name { font-size:22px; font-weight:700; }
#company { font-size:15px; font-weight:600; color:#bac2de; }
#number { font-size:12px; color:#9399b2; }
#timer { font-size:13px; font-weight:600; color:#89b4fa; }
QPushButton { background:rgba(255,255,255,20); border:none; border-radius:8px; color:#cdd6f4;
              padding:7px 10px; font-family:'Segoe UI','Segoe UI Emoji'; font-size:12px; }
QPushButton:hover { background:rgba(255,255,255,42); }
QPushButton:disabled { color:#6c7086; }
#close { background:transparent; font-size:14px; padding:2px 6px; }
#close:hover { background:rgba(243,139,168,60); }
"""


_STACK: list = []          # every CallOverlay that was placed; pruned when its Qt object is gone
_SEQ = itertools.count(1)  # creation order, so the newest missed card can sit nearest to the bottom
STACK_GAP = 10
INCOMING_GREEN, OUTGOING_MAUVE, ANSWERED_BLUE, MISSED_RED = "#a6e3a1", "#cba6f7", "#89b4fa", "#f38ba8"


# ---- placement: corner + offset, optional docking next to Bria's toast ---------------------------------------
_POSITIONS = ("bottom_right", "bottom_left", "top_right", "top_left")
_PLACEMENT = {
    "position": "bottom_right",
    "offset_x": -380,       # added to x: negative = to the left
    "offset_y": 0,          # added to y: negative = up
    "dock_to_bria": False,  # True = sit next to Bria's toast when one is on screen (offsets apply otherwise)
    "bria_process_regex": r"(?i)bria",
    "dock_gap_px": 12,
}
_CONFIGURED = False
_DOCK_INTERVAL_MS = 300     # re-check only while a live call card is visible and docking is on
_DOCK_TIMER = None
_LAST_ANCHOR = object()     # last anchor that was logged (only state changes are logged)


def configure_placement(ui: dict) -> None:
    """Apply the "ui" config block. Idempotent and cheap; called automatically on first use."""
    global _CONFIGURED
    d = _PLACEMENT
    pos = str(ui.get("overlay_position", d["position"])).strip().lower()
    try:
        d.update(
            position=pos if pos in _POSITIONS else "bottom_right",
            offset_x=int(ui.get("offset_x", d["offset_x"])),
            offset_y=int(ui.get("offset_y", d["offset_y"])),
            dock_to_bria=bool(ui.get("dock_to_bria", d["dock_to_bria"])),
            bria_process_regex=str(ui.get("bria_process_regex") or d["bria_process_regex"]),
            dock_gap_px=int(ui.get("dock_gap_px", d["dock_gap_px"])),
        )
    except (TypeError, ValueError):
        log.warning("invalid overlay placement settings in config.json, using the defaults", exc_info=True)
    _CONFIGURED = True
    log.info("overlay placement: %s offset=(%d,%d) dock_to_bria=%s", d["position"], d["offset_x"],
             d["offset_y"], d["dock_to_bria"])


def _ensure_configured() -> None:
    if _CONFIGURED:
        return
    try:
        configure_placement(config.load().get("ui") or {})
    except Exception:
        log.warning("could not read the placement settings, using the defaults", exc_info=True)
        globals()["_CONFIGURED"] = True


if sys.platform == "win32":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.GetWindowLongW.restype = ctypes.c_long
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                     ctypes.POINTER(wintypes.DWORD)]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

_GWL_EXSTYLE, _WS_EX_TOPMOST, _PROCESS_QUERY_LIMITED_INFORMATION = -20, 0x0008, 0x1000
_TOAST_MAX_W, _TOAST_MAX_H = 640, 420     # logical px: anything bigger is Bria's main window, not its toast


def _exe_name(pid: int, cache: dict) -> str:
    if pid in cache:
        return cache[pid]
    name = ""
    h = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if h:
        try:
            buf = ctypes.create_unicode_buffer(520)
            size = wintypes.DWORD(len(buf))
            if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                name = os.path.basename(buf.value)
        finally:
            _kernel32.CloseHandle(h)
    cache[pid] = name
    return name


def find_bria_toast(screen):
    """Bria's visible, always-on-top, toast-sized window on `screen` (QScreen), as a QRect in Qt logical
    coordinates; the one nearest the bottom-right corner. None if none / not Windows / any error.
    One EnumWindows pass, typically well under 2 ms. The px -> logical conversion assumes the primary screen
    (origin 0,0), which is the screen the cards use."""
    if sys.platform != "win32" or screen is None:
        return None
    try:
        rx = re.compile(_PLACEMENT["bria_process_regex"])
        dpr = screen.devicePixelRatio() or 1.0     # Qt 6 is per-monitor DPI aware: GetWindowRect = physical px
        area = screen.geometry()
        own_pid = os.getpid()
        cache, hits = {}, []

        def cb(hwnd, _lparam):
            try:
                if not _user32.IsWindowVisible(hwnd) or _user32.IsIconic(hwnd):
                    return True
                if not (_user32.GetWindowLongW(hwnd, _GWL_EXSTYLE) & _WS_EX_TOPMOST):
                    return True
                pid = wintypes.DWORD()
                _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value == own_pid or not rx.search(_exe_name(pid.value, cache)):
                    return True
                r = wintypes.RECT()
                if not _user32.GetWindowRect(hwnd, ctypes.byref(r)):
                    return True
                q = QRect(round(r.left / dpr), round(r.top / dpr),
                          round((r.right - r.left) / dpr), round((r.bottom - r.top) / dpr))
                if 40 <= q.width() <= _TOAST_MAX_W and 30 <= q.height() <= _TOAST_MAX_H and area.intersects(q):
                    hits.append(q)
            except Exception:
                pass
            return True

        proc = _WNDENUMPROC(cb)                  # keep a reference for the duration of the call
        _user32.EnumWindows(proc, 0)
        return max(hits, key=lambda q: q.right() + q.bottom()) if hits else None
    except Exception:
        log.debug("find_bria_toast failed", exc_info=True)
        return None


def _ensure_dock_timer():
    global _DOCK_TIMER
    if _DOCK_TIMER is None:
        _DOCK_TIMER = QTimer()
        _DOCK_TIMER.setInterval(_DOCK_INTERVAL_MS)
        _DOCK_TIMER.timeout.connect(_dock_tick)
    if not _DOCK_TIMER.isActive():
        _DOCK_TIMER.start()


def _dock_tick():
    # Follows the toast while it appears / slides in / disappears (answer, hang-up). Stops by itself when no live
    # call card is left, so missed-call cards that stay for hours cost nothing.
    if not _PLACEMENT["dock_to_bria"] or not any(isValid(w) and w.isVisible() and w._rank == 0 for w in _STACK):
        _DOCK_TIMER.stop()
        return
    CallOverlay._relayout()


def format_duration(seconds: int) -> str:
    """00:15, 01:42, 1:02:03"""
    seconds = max(0, int(seconds))
    h, rest = divmod(seconds, 3600)
    m, sec = divmod(rest, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


class CallOverlay(QWidget):
    closed = Signal()
    WIDTH = 340

    def __init__(self, margin: int = 16, answered_fade_ms: int = 2500, close_missed_on_action: bool = True):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedWidth(self.WIDTH)
        self.setStyleSheet(QSS)
        self._margin, self._fade_ms = margin, answered_fade_ms
        self._number, self._url = "", ""
        self._anim = None
        self._closing = False
        self._found = False          # the card shows a found caller (only then may a company line be added)
        self._answered = False       # in call: live duration counter
        self._answered_at = None     # time.monotonic() of the answer
        self._direction = "in"       # "in" | "out"
        self._missed, self._missed_count, self._when = False, 0, ""
        self._close_on_action = close_missed_on_action
        self._rank = 0               # stack position: 0 = live call / notice (bottom), 1 = missed card (above)
        self._seq = next(_SEQ)
        self._tick = QTimer(self)    # drives the duration label
        self._tick.setInterval(250)
        self._tick.timeout.connect(self._update_timer)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.end)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 16)
        root.setSpacing(4)
        head = QHBoxLayout()
        self.status = QLabel("INCOMING CALL")
        self.status.setObjectName("status")
        head.addWidget(self.status)
        head.addStretch()
        self.lbl_timer = QLabel()
        self.lbl_timer.setObjectName("timer")
        self.lbl_timer.setVisible(False)
        head.addWidget(self.lbl_timer)
        self.btn_close = QPushButton("✕")
        self.btn_close.setObjectName("close")
        self.btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_close.clicked.connect(self.end)
        head.addWidget(self.btn_close)
        root.addLayout(head)

        # top to bottom: lbl_name (line 1), lbl_company (line 2), lbl_number (line 3) -- see _lines
        self.lbl_name = QLabel()
        self.lbl_name.setObjectName("name")
        self.lbl_number = QLabel()
        self.lbl_number.setObjectName("number")
        self.lbl_company = QLabel()
        self.lbl_company.setObjectName("company")
        for w in (self.lbl_name, self.lbl_company, self.lbl_number):
            w.setWordWrap(True)
            root.addWidget(w)
        for w in (self.lbl_name, self.lbl_number):           # name / number can be selected with the mouse
            w.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.actions = QWidget()
        row = QHBoxLayout(self.actions)
        row.setContentsMargins(0, 8, 0, 0)
        row.setSpacing(8)
        self.btn_crm = QPushButton("🔍 MiniCRM")
        self.btn_google = QPushButton("🌐 Google")
        self.btn_copy = QPushButton("📋 Copy")
        for b in (self.btn_crm, self.btn_google, self.btn_copy):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            row.addWidget(b)
        self.btn_crm.clicked.connect(self._open_crm)
        self.btn_google.clicked.connect(self._open_google)
        self.btn_copy.clicked.connect(self._copy)
        root.addWidget(self.actions)

    # ---- painting / placement -------------------------------------------------------------
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(30, 30, 46, 235))          # #1e1e2e, slightly translucent
        p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 16, 16)

    def _place(self):
        self.adjustSize()
        if not any(w is self for w in _STACK):
            _STACK.append(self)
        CallOverlay._relayout(self)
        if _PLACEMENT["dock_to_bria"] and self._rank == 0:
            _ensure_dock_timer()

    @staticmethod
    def _relayout(extra=None):
        """Stack all visible cards: live call nearest the anchor, missed cards beyond it, newest first.
        Anchor = Bria's toast (docked to its left edge, bottoms aligned) when dock_to_bria is on and a toast is
        found; otherwise the configured corner of the usable screen area (excludes the taskbar) + offset_x/_y.
        `extra` is a card that is placed just before it is shown."""
        global _LAST_ANCHOR
        _ensure_configured()
        _STACK[:] = [w for w in _STACK if isValid(w)]
        live = [w for w in _STACK if w is extra or w.isVisible()]
        if not live:
            return
        live.sort(key=lambda w: (w._rank, -w._seq))
        scr = QGuiApplication.primaryScreen()
        area = scr.availableGeometry()
        p, m, width = _PLACEMENT, live[0]._margin, CallOverlay.WIDTH

        toast = find_bria_toast(scr) if p["dock_to_bria"] else None
        anchor = toast.getRect() if toast is not None else p["position"]
        if anchor != _LAST_ANCHOR:
            log.info("overlay anchor: %s", f"Bria toast {anchor}" if toast is not None else anchor)
            _LAST_ANCHOR = anchor
        if toast is not None:
            gap = p["dock_gap_px"]
            if toast.left() - gap - width >= area.left():   # room on the left: side by side, bottoms aligned
                x, edge, downward = toast.left() - gap - width, toast.bottom() + 1, False
            else:                                           # no room: on top of the toast
                x, edge, downward = toast.right() + 1 - width, toast.top() - gap, False
        else:
            pos = p["position"]
            x = area.left() + m if pos.endswith("left") else area.right() + 1 - width - m
            downward = pos.startswith("top")
            edge = area.top() + m if downward else area.bottom() + 1 - m
            x, edge = x + p["offset_x"], edge + p["offset_y"]

        x = max(area.left(), min(x, area.right() + 1 - width))   # never off screen
        y = edge
        for i, w in enumerate(live):
            h = w.height()
            if downward:
                y += STACK_GAP if i else 0
                w.move(x, max(area.top(), min(y, area.bottom() + 1 - h)))
                y += h
            else:
                y -= (STACK_GAP if i else 0) + h
                w.move(x, max(area.top(), min(y, area.bottom() + 1 - h)))

    def _fade(self, to: float, ms: int, then=None):
        if self._anim:
            self._anim.stop()
        self._anim = QPropertyAnimation(self, b"windowOpacity", self)
        self._anim.setDuration(ms)
        self._anim.setStartValue(self.windowOpacity())
        self._anim.setEndValue(to)
        if then:
            self._anim.finished.connect(then)
        self._anim.start()

    def _lines(self, primary: str, secondary: str = "", third: str = ""):
        """Set the card text, top to bottom: primary (big, bold), secondary (medium: the company),
        third (small, muted: the number). An empty secondary / third line is hidden.
        Does not resize: callers call _place() when needed."""
        self.lbl_name.setText(primary)
        self.lbl_company.setText(secondary)
        self.lbl_company.setVisible(bool(secondary))
        self.lbl_number.setText(third)
        self.lbl_number.setVisible(bool(third))

    # ---- states ----------------------------------------------------------------------------
    def show_ringing(self, number: str, direction: str = "in"):
        """Instant card, before the lookup has answered: the number is the only thing known, so it is the
        big line; set_caller() then swaps in the name and moves the number below it.
        direction "out" = a call we dial: the same card with an OUTGOING CALL status."""
        self._number, self._direction = number, ("out" if direction == "out" else "in")
        self._found, self._answered, self._missed = False, False, False
        self._answered_at = None
        self.lbl_timer.setVisible(False)
        if self._direction == "out":
            self.status.setText("● OUTGOING CALL")
            self.status.setStyleSheet(f"color:{OUTGOING_MAUVE};")
        else:
            self.status.setText("● INCOMING CALL")
            self.status.setStyleSheet(f"color:{INCOMING_GREEN};")
        self._lines(number or "Unknown number", "", "Looking up…")
        self.btn_crm.setEnabled(False)
        self.actions.setVisible(True)
        self.setWindowOpacity(0.0)
        self._place()
        self.show()
        self._fade(1.0, 150)

    def set_caller(self, info: CallerInfo):
        if self._closing:
            return
        self._found = info.found
        if info.found:
            self._lines(info.name, info.company, self._number)
            self._url = info.profile_url
            self.btn_crm.setEnabled(bool(self._url))
        elif info.error:
            # the lookup itself failed: the caller is NOT known to be unknown, so keep the number as the big line
            self._lines(self._number or "Unknown number", "Lookup failed", info.error)
        else:
            self._lines(UNKNOWN_CALLER, "", self._number)
        self._place()

    def set_company(self, company: str):
        """Stage 2: the company arrives after the name and pops in on the second line, between name and
        number. Ignored when the card is closing or not showing a found caller (looking up / unknown / error)."""
        if self._closing or not self._found or not company:
            return
        self.lbl_company.setText(company)
        self.lbl_company.setVisible(True)
        self._place()

    def set_answered(self):
        """The call was picked up: the card stays open and counts the call duration up until end() is called."""
        if self._closing or self._missed:
            return
        self._answered = True
        self._answered_at = time.monotonic()
        self._timer.stop()
        self.status.setText("● IN CALL")
        self.status.setStyleSheet(f"color:{ANSWERED_BLUE};")
        self.lbl_timer.setText(format_duration(0))
        self.lbl_timer.setVisible(True)
        self._tick.start()
        self._place()

    def _update_timer(self):
        if self._answered_at is None or self._closing:
            return
        text = format_duration(time.monotonic() - self._answered_at)
        if text != self.lbl_timer.text():
            self.lbl_timer.setText(text)

    def elapsed_s(self) -> int:
        """Seconds since the call was answered (0 when it was not)."""
        return 0 if self._answered_at is None else int(time.monotonic() - self._answered_at)

    def set_missed(self, count: int = 1, when: str = ""):
        """The caller gave up before we answered: the card stays until the user closes it. `count` > 1 =
        repeated missed calls from the same number (shown as x2, x3...)."""
        if self._closing:
            return
        self._missed, self._missed_count = True, max(1, int(count))
        self._answered, self._answered_at = False, None
        self._tick.stop()
        self._timer.stop()
        self.lbl_timer.setVisible(False)
        self._when = when or time.strftime("%H:%M")
        self._rank, self._seq = 1, next(_SEQ)           # above the live call, newest missed card nearest to it
        times = f" ×{self._missed_count}" if self._missed_count > 1 else ""
        self.status.setText(f"● MISSED CALL{times} · {self._when}")
        self.status.setStyleSheet(f"color:{MISSED_RED};")
        self.actions.setVisible(True)
        self._fade(1.0, 120)
        self._place()

    @property
    def number(self) -> str:
        return self._number

    @property
    def is_missed(self) -> bool:
        return self._missed

    @property
    def missed_count(self) -> int:
        return self._missed_count

    def show_notice(self, title: str, text: str, ms: int = 7000):
        self.status.setText("⚠ NOTICE")
        self.status.setStyleSheet("color:#f9e2af;")
        self._lines(title, text)
        self.lbl_name.setStyleSheet("font-size:15px; font-weight:600;")    # a notice title is not a caller name
        self.actions.setVisible(False)
        self.setWindowOpacity(0.0)
        self._place()
        self.show()
        self._fade(1.0, 150)
        self._timer.start(ms)

    def end(self):
        """Call ended / missed / user closed: fade out and destroy."""
        if self._closing:
            return
        self._closing = True
        self._timer.stop()
        self._tick.stop()
        self._fade(0.0, 180, self._finish)

    def _finish(self):
        self.close()
        CallOverlay._relayout()           # the cards above this one move down
        self.closed.emit()
        self.deleteLater()

    def _open_crm(self):
        if self._url:
            QDesktopServices.openUrl(QUrl(self._url))
            self._acted()

    def _open_google(self):
        QDesktopServices.openUrl(QUrl("https://www.google.com/search?q=" + quote(self._number)))
        self._acted()

    def _acted(self):
        """A missed-call card is dealt with once the user opened the contact or searched the number."""
        if self._missed and self._close_on_action:
            self.end()

    def _copy(self):
        QApplication.clipboard().setText(self._number)
        self.btn_copy.setText("✓ Copied")
        QTimer.singleShot(1200, lambda: self.btn_copy.setText("📋 Copy"))
