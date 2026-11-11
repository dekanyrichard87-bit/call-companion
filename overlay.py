"""Frameless, always-on-top, dark glass-style call card (bottom-right corner by default).

Card text, top to bottom (see CallOverlay._lines):
    line 1  big, bold         caller's NAME (while the lookup runs: the number)
    line 2  medium            COMPANY name (hidden when there is none; also the "Lookup failed" / notice text)
    line 3  small, muted      the PHONE NUMBER (while the lookup runs: "Looking up..."; after a failed lookup: the error)
An empty line 2 or 3 is hidden, so without a company the number sits directly below the name.

Card states (v0.7.0; the header shows an animated icon and the state in Hungarian):
    BEJÖVŐ HÍVÁS / KIMENŐ HÍVÁS     ringing / dialling: pulsing glow around the card, ripples + a short
                                    handset "wiggle" in the icon (text and buttons never move)
    HÍVÁSBAN  mm:ss                 answered: animated sound-wave bars, live duration counter
    NEM FOGADOTT HÍVÁS [×N] · hh:mm stays until closed; deep burgundy tint and accent bar
    HÍVÁS BEFEJEZVE · mm:ss (etc.)  after the call ends (or after an action on a missed card) the card stays
                                    ui.end_linger_ms (default 3 s) with the outcome, then fades out; the timer pauses
                                    while the mouse is over the card
Animations run on one 40 ms timer per card and only while it rings or a call is live.

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
import math
import os
import re
import sys
import time
from ctypes import wintypes
from urllib.parse import quote

from PySide6.QtCore import QPointF, QPropertyAnimation, QRect, QRectF, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from shiboken6 import isValid   # ships with PySide6: False once the C++ side of a widget is deleted

import config
from crm import CallerInfo

log = logging.getLogger("overlay")

UNKNOWN_CALLER = "Ismeretlen hívó"

QSS = """
QLabel { color:#cdd6f4; background:transparent; font-family:'Segoe UI'; }
#status { font-size:11px; font-weight:700; letter-spacing:1px; }
#name { font-size:22px; font-weight:700; }
#company { font-size:15px; font-weight:600; color:#bac2de; }
#number { font-size:12px; color:#9399b2; }
#timer { font-size:13px; font-weight:600; color:#89b4fa; }
QPushButton { background:rgba(255,255,255,20); border:none; border-radius:8px; color:#cdd6f4;
              padding:7px 10px; font-family:'Segoe UI','Segoe UI Emoji'; font-size:12px; }
QPushButton:hover { background:rgba(255,255,255,42); }
QPushButton:disabled { color:#6c7086; }
QPushButton#done { background:rgba(166,227,161,55); color:#a6e3a1; }
QPushButton#busy { color:#9399b2; }
#close { background:transparent; font-size:14px; padding:2px 6px; }
#close:hover { background:rgba(243,139,168,60); }
"""


_STACK: list = []          # every CallOverlay that was placed; pruned when its Qt object is gone
_SEQ = itertools.count(1)  # creation order, so the newest missed card can sit nearest to the bottom
GLOW = 6                   # transparent margin around the card for the pulsing glow (px on each side)
STACK_GAP = 2              # gap between two card windows; the visible gap is STACK_GAP + 2 * GLOW
INCOMING_GREEN, OUTGOING_MAUVE, ANSWERED_BLUE, MISSED_RED = "#a6e3a1", "#cba6f7", "#89b4fa", "#f38ba8"
HOLD_PEACH, NOTICE_YELLOW, ENDED_GREY, MISSED_BURGUNDY = "#fab387", "#f9e2af", "#7f849c", "#8c2f45"
ANIM_MS = 40               # 25 fps, only while a card rings or a call is live


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




def _alpha(color: str, a: int) -> QColor:
    c = QColor(color)
    c.setAlpha(max(0, min(255, int(a))))
    return c


class StateIcon(QWidget):
    """22 px animated state icon in the card header (painted, no image files).
    ring      ripples + a handset that wiggles for 0.45 s every 1.6 s (incoming / outgoing ringing)
    talk      four sound-wave bars (call in progress)
    hold      a breathing pause sign (v0.8.0)
    missed    burgundy disc with a cross        ended / notice / transfer: static glyphs"""
    SIZE = 22

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self._mode, self._color, self._t0 = "ring", QColor(INCOMING_GREEN), time.monotonic()

    def set_mode(self, mode: str, color: str):
        self._mode, self._color, self._t0 = mode, QColor(color), time.monotonic()
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        t = time.monotonic() - self._t0
        c, mode = self._color, self._mode
        cx = cy = self.SIZE / 2
        if mode == "ring":
            p.setBrush(Qt.BrushStyle.NoBrush)
            for k in range(2):                                   # two expanding ripples
                ph = (t / 1.4 + k * 0.5) % 1.0
                p.setPen(QPen(_alpha(c.name(), 170 * (1 - ph)), 1.4))
                r = 4 + ph * 6.5
                p.drawEllipse(QPointF(cx, cy), r, r)
            cyc = t % 1.6                                        # handset wiggle burst
            ang = 16 * math.sin(cyc * 42) * (1 - cyc / 0.45) if cyc < 0.45 else 0
            p.translate(cx, cy)
            p.rotate(ang)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            p.drawEllipse(QPointF(0, 0), 5.2, 5.2)
            p.setPen(QPen(QColor(BASE_BG), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawArc(QRectF(-3, -3, 6, 6), 200 * 16, 140 * 16)  # tiny handset curve
        elif mode == "talk":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            for i in range(4):
                h = 4 + 10 * (0.5 + 0.5 * math.sin(t * 7.0 + i * 1.3)) * (0.6 + 0.4 * math.sin(t * 2.3 + i))
                x = 3.5 + i * 4.5
                p.drawRoundedRect(QRectF(x, cy - h / 2, 2.6, h), 1.3, 1.3)
        elif mode == "hold":
            a = 150 + 105 * (0.5 + 0.5 * math.sin(t * 2.6))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_alpha(c.name(), a))
            p.drawRoundedRect(QRectF(6, 5, 3.4, 12), 1.5, 1.5)
            p.drawRoundedRect(QRectF(12.6, 5, 3.4, 12), 1.5, 1.5)
        else:                                                    # static: missed / ended / notice / done
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_alpha(c.name(), 60 if mode != "missed" else 255))
            p.drawEllipse(QPointF(cx, cy), 9, 9)
            pen = QPen(QColor("#f5c2cf") if mode == "missed" else c, 1.8, Qt.PenStyle.SolidLine,
                       Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            if mode == "missed":
                p.drawLine(QPointF(7.5, 7.5), QPointF(14.5, 14.5))
                p.drawLine(QPointF(14.5, 7.5), QPointF(7.5, 14.5))
            elif mode == "done":
                p.drawLine(QPointF(7, 11.5), QPointF(10, 14.5))
                p.drawLine(QPointF(10, 14.5), QPointF(15.5, 8))
            elif mode == "notice":
                p.drawLine(QPointF(11, 6.5), QPointF(11, 12.5))
                p.drawPoint(QPointF(11, 15.5))
            else:                                                # ended: handset down (short bar)
                p.drawLine(QPointF(6.5, 12), QPointF(15.5, 12))
        p.end()


BASE_BG = "#1e1e2e"


class CallOverlay(QWidget):
    closed = Signal()
    WIDTH = 340                       # visible card width
    OUTER = WIDTH + 2 * GLOW          # window width (card + glow margin)
    LINGER_DEFAULT_MS = 3000

    def __init__(self, margin: int = 16, answered_fade_ms: int = 2500, close_missed_on_action: bool = True,
                 end_linger_ms: int = LINGER_DEFAULT_MS):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedWidth(self.OUTER)
        self.setStyleSheet(QSS)
        self._margin, self._fade_ms = margin, answered_fade_ms
        self._linger_ms = max(0, int(end_linger_ms))
        self._number, self._url = "", ""
        self._anim = None
        self._closing = False        # fading out (or about to): no more state changes
        self._ended = False          # lingering with the outcome before the fade
        self._found = False          # the card shows a found caller (only then may a company line be added)
        self._answered = False       # in call: live duration counter
        self._answered_at = None     # time.monotonic() of the answer
        self._direction = "in"       # "in" | "out"
        self._missed, self._missed_count, self._when = False, 0, ""
        self._close_on_action = close_missed_on_action
        self._rank = 0               # stack position: 0 = live call / notice (bottom), 1 = missed card (above)
        self._seq = next(_SEQ)
        self._visual = "ring"        # ring | talk | hold | missed | ended | notice  (drives paintEvent)
        self._accent = INCOMING_GREEN
        self._t0 = time.monotonic()
        self._hover = False
        self._tick = QTimer(self)    # drives the duration label
        self._tick.setInterval(250)
        self._tick.timeout.connect(self._update_timer)
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(ANIM_MS)
        self._anim_timer.timeout.connect(self._animate)
        self._timer = QTimer(self)   # notice timeout / linger before the fade
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._linger_done)

        root = QVBoxLayout(self)
        root.setContentsMargins(18 + GLOW, 13 + GLOW, 18 + GLOW, 15 + GLOW)
        root.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.icon = StateIcon(self)
        head.addWidget(self.icon)
        self.status = QLabel("BEJÖVŐ HÍVÁS")
        self.status.setObjectName("status")
        head.addWidget(self.status)
        head.addStretch()
        self.lbl_timer = QLabel()
        self.lbl_timer.setObjectName("timer")
        self.lbl_timer.setVisible(False)
        head.addWidget(self.lbl_timer)
        self.btn_close = QPushButton("✕")
        self.btn_close.setObjectName("close")
        self.btn_close.setToolTip("Bezárás")
        self.btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_close.clicked.connect(self.close_now)
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
        self.btn_copy = QPushButton("📋 Másolás")
        self.btn_crm.setToolTip("A hívó megnyitása a MiniCRM-ben")
        self.btn_google.setToolTip("A szám keresése a Google-ben")
        self.btn_copy.setToolTip("A telefonszám másolása a vágólapra")
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
        card = QRectF(self.rect()).adjusted(GLOW + 0.5, GLOW + 0.5, -GLOW - 0.5, -GLOW - 0.5)
        v, accent = self._visual, self._accent
        if v == "ring":                                          # pulsing glow (1.2 s period)
            t = time.monotonic() - self._t0
            pulse = 0.5 + 0.5 * math.sin(t * 2 * math.pi / 1.2)
            for i in range(GLOW, 0, -1):
                p.setPen(QPen(_alpha(accent, (14 + 46 * pulse) * (1 - i / (GLOW + 1))), 1.0))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(card.adjusted(-i, -i, i, i), 16 + i, 16 + i)
            border = _alpha(accent, 90 + 130 * pulse)
        elif v in ("talk", "hold"):
            border = _alpha(accent, 110)
        elif v == "missed":
            border = _alpha(MISSED_BURGUNDY, 230)
        elif v == "notice":
            border = _alpha(NOTICE_YELLOW, 90)
        else:
            border = QColor(255, 255, 255, 30)
        bg = QColor(52, 24, 34, 242) if v == "missed" else QColor(30, 30, 46, 238)   # #1e1e2e / burgundy tint
        p.setBrush(bg)
        p.setPen(QPen(border, 1.4))
        p.drawRoundedRect(card, 16, 16)
        if v in ("missed", "talk", "hold", "ring"):              # thin accent bar on the left edge
            bar = MISSED_BURGUNDY if v == "missed" else accent
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_alpha(bar, 255 if v == "missed" else 150))
            p.drawRoundedRect(QRectF(card.left() + 1.5, card.top() + 18, 3.2, card.height() - 36), 1.6, 1.6)

    def _set_visual(self, visual: str, accent: str, text: str, icon_mode: str | None = None):
        self._visual, self._accent = visual, accent
        self._t0 = time.monotonic()
        self.status.setText(text)
        color = "#f5a3b5" if visual == "missed" else (ENDED_GREY if visual == "ended" else accent)
        self.status.setStyleSheet(f"color:{color};")
        self.icon.set_mode(icon_mode or visual, MISSED_BURGUNDY if visual == "missed" else accent)
        if visual in ("ring", "talk", "hold"):
            self._anim_timer.start()
        else:
            self._anim_timer.stop()
        self.update()

    def _animate(self):
        self.icon.update()
        if self._visual == "ring":
            self.update()                     # the glow pulses: repaint the whole card (only while ringing)

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
        # The window is GLOW px bigger than the visible card on every side: shrink the margin by GLOW so the
        # visible card edge stays where it was before v0.7.0.
        p, m, width = _PLACEMENT, live[0]._margin - GLOW, CallOverlay.OUTER

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
        direction "out" = a call we dial: the same card with a KIMENŐ HÍVÁS header."""
        self._number, self._direction = number, ("out" if direction == "out" else "in")
        self._found, self._answered, self._missed = False, False, False
        self._answered_at = None
        self.lbl_timer.setVisible(False)
        if self._direction == "out":
            self._set_visual("ring", OUTGOING_MAUVE, "KIMENŐ HÍVÁS")
        else:
            self._set_visual("ring", INCOMING_GREEN, "BEJÖVŐ HÍVÁS")
        self._lines(number or "Ismeretlen szám", "", "Keresés a MiniCRM-ben…")
        self.btn_crm.setEnabled(False)
        self.actions.setVisible(True)
        self.setWindowOpacity(0.0)
        self._place()
        self.show()
        self._fade(1.0, 160)

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
            self._lines(self._number or "Ismeretlen szám", "Sikertelen keresés", info.error)
        else:
            self._lines(UNKNOWN_CALLER, "", self._number)
        self.btn_crm.setToolTip("A hívó megnyitása a MiniCRM-ben" if self._url
                                else "Ez a hívó nincs meg a MiniCRM-ben")
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
        """The call was picked up: the card stays open and counts the call duration up until the call ends."""
        if self._closing or self._ended or self._missed:
            return
        self._answered = True
        self._answered_at = time.monotonic()
        self._timer.stop()
        self._set_visual("talk", ANSWERED_BLUE, "HÍVÁSBAN")
        self.lbl_timer.setText(format_duration(0))
        self.lbl_timer.setVisible(True)
        self._tick.start()
        self._place()

    def _update_timer(self):
        if self._answered_at is None or self._closing or self._ended:
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
        self._set_visual("missed", MISSED_RED, f"NEM FOGADOTT HÍVÁS{times} · {self._when}")
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
        self._set_visual("notice", NOTICE_YELLOW, "FIGYELMEZTETÉS", "notice")
        self._lines(title, text)
        self.lbl_name.setStyleSheet("font-size:15px; font-weight:600;")    # a notice title is not a caller name
        self.actions.setVisible(False)
        self.setWindowOpacity(0.0)
        self._place()
        self.show()
        self._fade(1.0, 150)
        self._ended = True                     # a notice only lingers, then fades (hover pauses it)
        self._timer.start(ms)

    # ---- ending: linger with the outcome, then fade ------------------------------------------------
    def finish(self, text: str = "HÍVÁS BEFEJEZVE", linger_ms: int | None = None, icon: str = "ended",
               accent: str = ENDED_GREY):
        """The call is over (or the user acted on a missed card): show `text` for linger_ms (default
        ui.end_linger_ms), then fade out. The counter freezes at the final duration. Hovering pauses the timer."""
        if self._closing or self._ended:
            return
        self._ended = True
        self._tick.stop()
        if self._answered_at is not None:
            self.lbl_timer.setText(format_duration(time.monotonic() - self._answered_at))
            self.lbl_timer.setStyleSheet(f"color:{ENDED_GREY};")
        self._set_visual("ended", accent, text, icon)
        self._place()
        ms = self._linger_ms if linger_ms is None else max(0, int(linger_ms))
        if ms <= 0:
            self.end()
        elif not self._hover:
            self._timer.start(ms)

    def _linger_done(self):
        if self._hover and self._ended:
            return                              # leaveEvent restarts a short timer
        self.end()

    def enterEvent(self, e):
        self._hover = True
        if self._ended and self._timer.isActive():
            self._timer.stop()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        if self._ended and not self._closing:
            self._timer.start(1500)
        super().leaveEvent(e)

    @property
    def is_ended(self) -> bool:
        return self._ended

    def close_now(self):
        """✕ button: no linger."""
        self.end()

    def end(self):
        """Fade out and destroy (immediately, no linger)."""
        if self._closing:
            return
        self._closing = True
        self._timer.stop()
        self._tick.stop()
        self._anim_timer.stop()
        self._fade(0.0, 200, self._finish)

    def _finish(self):
        self.close()
        CallOverlay._relayout()           # the cards above this one move down
        self.closed.emit()
        self.deleteLater()

    # ---- actions -------------------------------------------------------------------------------------
    def _open_crm(self):
        if self._url:
            QDesktopServices.openUrl(QUrl(self._url))
            self._acted("MINICRM MEGNYITVA")

    def _open_google(self):
        QDesktopServices.openUrl(QUrl("https://www.google.com/search?q=" + quote(self._number)))
        self._acted("GOOGLE KERESÉS MEGNYITVA")

    def _acted(self, text: str):
        """A missed-call card is dealt with once the user opened the contact or searched the number: it confirms
        the action for a moment, then goes."""
        if self._missed and self._close_on_action:
            self.finish("✓ " + text, min(self._linger_ms or 1800, 1800), "done", INCOMING_GREEN)

    def _copy(self):
        QApplication.clipboard().setText(self._number)
        self._flash(self.btn_copy, "✓ Másolva")

    def _flash(self, button: QPushButton, text: str, ms: int = 1300):
        """Micro-interaction: the button turns green with a confirmation, then restores itself."""
        orig = button.property("_orig") or button.text()
        button.setProperty("_orig", orig)
        button.setText(text)
        button.setObjectName("done")
        button.style().unpolish(button)
        button.style().polish(button)
        token = (button.property("_tok") or 0) + 1
        button.setProperty("_tok", token)

        def restore():
            if not isValid(button) or button.property("_tok") != token:
                return
            button.setText(orig)
            button.setObjectName("")
            button.setProperty("_orig", None)
            button.style().unpolish(button)
            button.style().polish(button)
        QTimer.singleShot(ms, restore)
