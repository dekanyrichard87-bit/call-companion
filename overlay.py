"""Frameless, always-on-top, dark glass-style call card in the bottom-right corner."""
from urllib.parse import quote

from PySide6.QtCore import QPropertyAnimation, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from crm import CallerInfo

QSS = """
QLabel { color:#cdd6f4; background:transparent; font-family:'Segoe UI'; }
#status { font-size:11px; font-weight:600; }
#number { font-size:22px; font-weight:600; }
#name { font-size:15px; }
#company { font-size:12px; color:#9399b2; }
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
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.end)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 16)
        root.setSpacing(6)
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

        self.lbl_number = QLabel()
        self.lbl_number.setObjectName("number")
        self.lbl_number.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.lbl_name = QLabel()
        self.lbl_name.setObjectName("name")
        self.lbl_name.setWordWrap(True)
        self.lbl_company = QLabel()
        self.lbl_company.setObjectName("company")
        self.lbl_company.setWordWrap(True)
        for w in (self.lbl_number, self.lbl_name, self.lbl_company):
            root.addWidget(w)

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

    # ---- states ----------------------------------------------------------------------------
    def show_ringing(self, number: str):
        self._number = number
        self.status.setText("● INCOMING CALL")
        self.status.setStyleSheet("color:#a6e3a1;")
        self.lbl_number.setText(number or "Unknown number")
        self.lbl_name.setText("Looking up…")
        self.lbl_company.setText("")
        self.lbl_company.setVisible(False)
        self.btn_crm.setEnabled(False)
        self.actions.setVisible(True)
        self.setWindowOpacity(0.0)
        self._place()
        self.show()
        self._fade(1.0, 150)

    def set_caller(self, info: CallerInfo):
        if self._closing:
            return
        if info.found:
            self.lbl_name.setText(info.name)
            self.lbl_company.setText(info.company)
            self.lbl_company.setVisible(bool(info.company))
            self._url = info.profile_url
            self.btn_crm.setEnabled(bool(self._url))
        elif info.error:
            self.lbl_name.setText("Lookup failed")
            self.lbl_company.setText(info.error)
            self.lbl_company.setVisible(True)
        else:
            self.lbl_name.setText("Unknown caller")
        self._place()

    def set_answered(self):
        if self._closing:
            return
        self.status.setText("● IN CALL")
        self.status.setStyleSheet("color:#89b4fa;")
        self.lbl_company.setVisible(False)
        self.actions.setVisible(False)
        self._place()
        self._timer.start(self._fade_ms)

    def show_notice(self, title: str, text: str, ms: int = 7000):
        self.status.setText("⚠ NOTICE")
        self.status.setStyleSheet("color:#f9e2af;")
        self.lbl_number.setText(title)
        self.lbl_number.setStyleSheet("font-size:15px;")
        self.lbl_name.setText(text)
        self.lbl_company.setVisible(False)
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
