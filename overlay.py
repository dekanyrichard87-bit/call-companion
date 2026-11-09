"""Frameless, always-on-top, dark glass-style call card in the bottom-right corner.

Card text, top to bottom (see CallOverlay._lines):
    line 1  big, bold         caller's NAME (while the lookup runs: the number)
    line 2  medium            COMPANY name (hidden when there is none; also the "Lookup failed" / notice text)
    line 3  small, muted      the PHONE NUMBER (while the lookup runs: "Looking up..."; after a failed lookup: the error)
An empty line 2 or 3 is hidden, so without a company the number sits directly below the name.
"""
from urllib.parse import quote

from PySide6.QtCore import QPropertyAnimation, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from crm import CallerInfo

UNKNOWN_CALLER = "Ismeretlen hívó"

QSS = """
QLabel { color:#cdd6f4; background:transparent; font-family:'Segoe UI'; }
#status { font-size:11px; font-weight:600; }
#name { font-size:22px; font-weight:700; }
#company { font-size:15px; font-weight:600; color:#bac2de; }
#number { font-size:12px; color:#9399b2; }
QPushButton { background:rgba(255,255,255,20); border:none; border-radius:8px; color:#cdd6f4;
              padding:7px 10px; font-family:'Segoe UI','Segoe UI Emoji'; font-size:12px; }
QPushButton:hover { background:rgba(255,255,255,42); }
QPushButton:disabled { color:#6c7086; }
#close { background:transparent; font-size:14px; padding:2px 6px; }
#close:hover { background:rgba(243,139,168,60); }
"""


class CallOverlay(QWidget):
    closed = Signal()
    WIDTH = 340

    def __init__(self, margin: int = 16, answered_fade_ms: int = 2500):
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
        self._answered = False       # compact "IN CALL" view
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
        self.btn_crm.clicked.connect(lambda: self._url and QDesktopServices.openUrl(QUrl(self._url)))
        self.btn_google.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl("https://www.google.com/search?q=" + quote(self._number))))
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
        screen = QGuiApplication.primaryScreen().availableGeometry()  # excludes taskbar
        self.move(screen.right() - self.width() - self._margin,
                  screen.bottom() - self.height() - self._margin)

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
    def show_ringing(self, number: str):
        """Instant card, before the lookup has answered: the number is the only thing known, so it is the
        big line; set_caller() then swaps in the name and moves the number below it."""
        self._number = number
        self._found, self._answered = False, False
        self.status.setText("● INCOMING CALL")
        self.status.setStyleSheet("color:#a6e3a1;")
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
        number. Ignored when the card is closing, in the compact in-call view, or not showing a found caller (looking up / unknown / error)."""
        if self._closing or self._answered or not self._found or not company:
            return
        self.lbl_company.setText(company)
        self.lbl_company.setVisible(True)
        self._place()

    def set_answered(self):
        if self._closing:
            return
        self._answered = True
        self.status.setText("● IN CALL")
        self.status.setStyleSheet("color:#89b4fa;")
        self.lbl_company.setVisible(False)
        self.actions.setVisible(False)
        self._place()
        self._timer.start(self._fade_ms)

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
        self._fade(0.0, 180, self._finish)

    def _finish(self):
        self.close()
        self.closed.emit()
        self.deleteLater()

    def _copy(self):
        QApplication.clipboard().setText(self._number)
        self.btn_copy.setText("✓ Copied")
        QTimer.singleShot(1200, lambda: self.btn_copy.setText("📋 Copy"))
