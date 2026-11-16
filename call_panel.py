"""Call panel inside the main window (v0.10.0-beta.3, design canvas "Hívás a főablakban – 0.10").

When the main window is the active, visible window, a call (incoming or outgoing) is shown here, between the status
card and HÍVÁSELŐZMÉNYEK, instead of the pop-up card. Same states and buttons as the card:

    ((•)) BEJÖVŐ HÍVÁS                 a kártya helyett, mert a főablak aktív   01:42  ✕
    Minta Anna                                              [ Felvesz ] [ Elutasít ]
    [Feszültség Kft.]  +36 30 123 4567                      (in call: [ Tartás ] [ Bontás ])
    [ Visszahívás ] [ M MiniCRM ] [ G Google ] [ Másolás ]   (Visszahívás only on a missed call)

How it works: the card (overlay.CallOverlay) is still created and keeps the whole state and every action, but it
stays hidden ("suppressed"). The panel mirrors the card (CallOverlay.snapshot(), on its `changed` signal) and its
buttons press the card's buttons, so answer / decline / hold / hang up / Visszahívás / MiniCRM / Google / copy work
exactly as on the card. When the main window loses the focus, is minimised or closed, `release()` shows the card at
once (and the panel goes away). A missed call stays here until it is handled; a new call takes the panel and the
missed one becomes a card. Átkapcsolás also moves the call to the card (the extension list lives there).
Nothing animates while the panel is hidden; while it shows a ringing / live call only its state icon is animated.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

import icons
import theme
from overlay import ANIM_CALL_MS, ANIM_MS, StateIcon

log = logging.getLogger("callcompanion.panel")

WHY = "a kártya helyett, mert a főablak aktív"


def _tone(widget, role: str) -> None:
    """theme.set_tone only when the tone changes (it re-polishes the widget)."""
    if (widget.property("tone") or "") != (role or ""):
        theme.set_tone(widget, role or None)


class CallPanel(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("callPanel")
        self._card = None
        self._snap: dict = {}
        self._icon_key = None
        self._anim = QTimer(self)
        self._anim.timeout.connect(lambda: self.icon.update())

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 12, 14, 12)
        root.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.icon = StateIcon(self)
        head.addWidget(self.icon)
        self.status = QLabel(objectName="pStatus")
        head.addWidget(self.status)
        head.addStretch(1)
        self.why = QLabel(WHY, objectName="pWhy")
        head.addWidget(self.why)
        self.timer = QLabel(objectName="pTimer")
        head.addWidget(self.timer)
        self.btn_close = QPushButton("", objectName="close")
        self.btn_close.setToolTip("Bezárás")
        head.addWidget(self.btn_close)
        root.addLayout(head)

        body = QHBoxLayout()
        body.setSpacing(14)
        left = QVBoxLayout()
        left.setSpacing(2)
        self.name = QLabel(objectName="pName")
        self.name.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        left.addWidget(self.name)
        line2 = QHBoxLayout()
        line2.setSpacing(8)
        self.tag = QLabel(objectName="pTag")
        self.tag.setMaximumWidth(220)
        self.number = QLabel(objectName="pNumber")
        self.number.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        line2.addWidget(self.tag)
        line2.addWidget(self.number)
        line2.addStretch(1)
        left.addLayout(line2)
        body.addLayout(left, 1)
        self.btn_answer = QPushButton("Felvesz", objectName="answer")
        self.btn_reject = QPushButton("Elutasít", objectName="reject")
        self.btn_hold = QPushButton("Tartás", objectName="ctl")
        self.btn_xfer = QPushButton("Átkapcsolás", objectName="ctl")
        self.btn_hangup = QPushButton("Bontás", objectName="reject")
        for b in (self.btn_answer, self.btn_reject, self.btn_hold, self.btn_xfer, self.btn_hangup):
            body.addWidget(b, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(body)

        self.hint = QLabel(objectName="pHint")
        self.hint.setWordWrap(True)
        root.addWidget(self.hint)

        acts = QHBoxLayout()
        acts.setSpacing(8)
        self.btn_callback = QPushButton("Visszahívás", objectName="callback")
        self.btn_crm = QPushButton("MiniCRM")
        self.btn_google = QPushButton("Google")
        self.btn_copy = QPushButton("Másolás")
        for b in (self.btn_callback, self.btn_crm, self.btn_google, self.btn_copy):
            acts.addWidget(b)
        acts.addStretch(1)
        root.addLayout(acts)

        tips = {self.btn_answer: "A hívás fogadása", self.btn_hold: "A hívás tartásba tétele",
                self.btn_xfer: "Átkapcsolás – a hívás a kártyára kerül, ott választhat kollégát",
                self.btn_hangup: "A hívás befejezése", self.btn_google: "A szám keresése a Google-ben",
                self.btn_copy: "A telefonszám másolása a vágólapra"}
        for b, t in tips.items():
            b.setToolTip(t)
        # every button presses the card's own button: the card does the work (and keeps the state)
        self._forward = {self.btn_answer: "btn_answer", self.btn_reject: "btn_reject", self.btn_hold: "btn_hold",
                         self.btn_hangup: "btn_hangup", self.btn_callback: "btn_callback", self.btn_crm: "btn_crm",
                         self.btn_google: "btn_google", self.btn_copy: "btn_copy", self.btn_close: "btn_close"}
        for b, attr in self._forward.items():
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, a=attr: self._press(a))
        self.btn_xfer.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_xfer.clicked.connect(lambda: self.release("transfer"))
        theme.changed.connect(self._apply_icons)
        self._apply_icons()
        self.hide()

    # ---- binding ---------------------------------------------------------------------------------------------------
    @property
    def has_call(self) -> bool:
        return self._card is not None

    @property
    def card(self):
        return self._card

    def bind(self, card) -> None:
        """Show this (suppressed) card's call here. A call already shown here moves to its card."""
        if card is self._card:
            return
        if self._card is not None:
            self.release("new call")
        self._card = card
        card.changed.connect(self.refresh)
        card.closed.connect(self._card_closed)
        self.refresh()
        self.show()

    def release(self, why: str = "") -> None:
        """The call leaves the panel and its card appears at once (window lost the focus / a new call / transfer)."""
        card = self._unbind()
        if card is not None:
            log.info("panel: %s -> card", why or "released")
            try:
                card.unsuppress()
            except RuntimeError:          # the card is already gone
                pass

    def drop(self) -> None:
        """Forget the card without showing it (it is being replaced / deleted)."""
        self._unbind()

    def _card_closed(self) -> None:
        self._unbind()

    def _unbind(self):
        card, self._card = self._card, None
        if card is not None:
            for sig, slot in ((card.changed, self.refresh), (card.closed, self._card_closed)):
                try:
                    sig.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        self._anim.stop()
        self._snap = {}
        self.hide()
        return card

    def _press(self, attr: str) -> None:
        card = self._card
        if card is None:
            return
        try:
            getattr(card, attr).click()
        except RuntimeError:
            self._unbind()

    # ---- mirror ----------------------------------------------------------------------------------------------------
    def refresh(self) -> None:
        card = self._card
        if card is None:
            return
        try:
            s = card.snapshot()
        except RuntimeError:
            self._unbind()
            return
        self._snap = s
        if (s["icon"], s["icon_role"]) != (self.icon._mode, self.icon._role):
            self.icon.set_mode(s["icon"], s["icon_role"])
        self.status.setText(s["status"])
        _tone(self.status, s["status_tone"])
        self.why.setVisible(not s["ended"])
        self.timer.setText(s["timer"])
        self.timer.setVisible(bool(s["timer"]))
        _tone(self.timer, s["timer_tone"])
        self.name.setText(s["name"])
        self.tag.setText(s["company"])
        self.tag.setVisible(bool(s["company"]))
        self.number.setText(s["number"])
        self.number.setVisible(bool(s["number"]))
        self.btn_answer.setVisible(s["ring"] and s["answer"])
        self.btn_reject.setVisible(s["ring"])
        self.btn_reject.setText(s["reject"])
        self.btn_hold.setVisible(s["controls"])
        self.btn_hold.setText(s["hold"])
        self.btn_xfer.setVisible(s["controls"] and s["xfer"])
        self.btn_hangup.setVisible(s["controls"])
        for b in (self.btn_answer, self.btn_reject, self.btn_hold, self.btn_xfer, self.btn_hangup):
            b.setEnabled(not s["busy"])
        self.btn_callback.setVisible(s["callback"])
        self.btn_callback.setEnabled(s["callback_on"])
        self.btn_callback.setToolTip(s["callback_tip"])
        for b in (self.btn_crm, self.btn_google, self.btn_copy):
            b.setVisible(s["actions"])
        self.btn_crm.setEnabled(s["crm_on"])
        self.btn_crm.setToolTip(s["crm_tip"])
        self.btn_copy.setText(s["copy"])
        done = "done" if s["copy_done"] else ""
        if self.btn_copy.objectName() != done:
            self.btn_copy.setObjectName(done)
            self.btn_copy.style().unpolish(self.btn_copy)
            self.btn_copy.style().polish(self.btn_copy)
        self.hint.setText(s["hint"])
        self.hint.setVisible(bool(s["hint"]))
        _tone(self.hint, s["hint_tone"])
        key = (bool(s["held"]), bool(s["copy_done"]))
        if key != self._icon_key:                 # icons only when they change (the timer refreshes every second)
            self._apply_icons()
        live = s["visual"] in ("ring", "talk", "hold") and not s["closing"]
        if live and self.isVisible():
            self._anim.start(ANIM_MS if s["visual"] == "ring" else ANIM_CALL_MS)
        else:
            self._anim.stop()
        self.update()

    def showEvent(self, e):
        super().showEvent(e)
        if self._snap.get("visual") in ("ring", "talk", "hold"):
            self._anim.start(ANIM_MS if self._snap["visual"] == "ring" else ANIM_CALL_MS)

    def hideEvent(self, e):
        self._anim.stop()
        super().hideEvent(e)

    def _apply_icons(self) -> None:
        s = self._snap
        self._icon_key = (bool(s.get("held")), bool(s.get("copy_done")))
        pairs = ((self.btn_close, "close", "text"), (self.btn_answer, "phone", "green"),
                 (self.btn_reject, "hangup", "red"), (self.btn_hangup, "hangup", "red"),
                 (self.btn_xfer, "transfer", "text"), (self.btn_crm, "crm", "text"),
                 (self.btn_google, "google", "text"), (self.btn_callback, "phone", "on_accent"),
                 (self.btn_hold, "play" if s.get("held") else "pause", "text"),
                 (self.btn_copy, "check" if s.get("copy_done") else "copy", "green" if s.get("copy_done") else "text"))
        for b, name, role in pairs:
            b.setIcon(icons.icon(name, role, disabled=None if b is self.btn_callback else "disabled"))
            b.setIconSize(QSize(15, 15))
        self.icon.update()
        self.update()

    def paintEvent(self, _):
        """Panel background (missed: burgundy tint), state-coloured border, accent bar on the left."""
        pal = theme.palette()
        s = self._snap
        v = s.get("visual", "ring")
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(3.5, 3.5, -3.5, -3.5)
        missed = v == "missed"
        role = "burgundy" if missed else s.get("accent", "green")
        acc = QColor(pal[role])
        if v == "ring":                                   # soft outer ring while ringing (static, no animation)
            halo = QColor(acc)
            halo.setAlpha(38)
            p.setPen(QPen(halo, 3))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r.adjusted(-1.5, -1.5, 1.5, 1.5), 15.5, 15.5)
        bg = QColor(pal.row_missed if missed else pal.surface)
        if v == "ended":
            border = QColor(*pal.card_edge)
        else:
            border = QColor(acc)
            border.setAlpha(230 if missed else 205)
        p.setPen(QPen(border, 1.4))
        p.setBrush(bg)
        p.drawRoundedRect(r, 14, 14)
        if v != "ended":
            bar = QColor(acc)
            bar.setAlpha(255 if missed else 150)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(bar)
            p.drawRoundedRect(QRectF(r.left() + 1.5, r.top() + 14, 3.2, r.height() - 28), 1.6, 1.6)
        p.end()
