"""Visszajelzés (v1.1.0): bug reports and ideas from the main window, straight to the author's e-mail.

The message goes to a Formspree form (FEEDBACK_ENDPOINT in version.py, or config "feedback.endpoint") as JSON with
"Accept: application/json", so no browser is needed. Formspree e-mails each submission to the form owner. The free
plan allows 50 submissions a month (checked 2026-11-19). Web3Forms was the first choice, but its free plan refuses
submissions that do not come from a browser (they need a paid plan + an IP safelist).

Nothing secret is in the app: a Formspree form id is public by design (it sits in every HTML form that uses it).

"Napló csatolása" adds the end of app.log with phone numbers and caller names masked (mask_log). The status report
(the same as "Hibajelentés másolása", no caller data) and the version are always added.

Without an endpoint, or when sending fails (no network, Formspree down, monthly limit), the full text goes to the
clipboard and the e-mail program opens with a short message (mailto: cannot carry attachments or long bodies).
"""
from __future__ import annotations

import logging
import platform
import re
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from PySide6.QtCore import QObject, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QVBoxLayout,
)

import theme
from version import FEEDBACK_EMAIL, FEEDBACK_ENDPOINT

log = logging.getLogger("callcompanion.feedback")

KINDS = (("bug", "Hiba"), ("idea", "Ötlet"), ("other", "Egyéb"))
LOG_LINES = 400                 # end of app.log that is attached
LOG_MAX_CHARS = 40_000

_PHONE = re.compile(r"(?<![\w.:-])\+?\d[\d ]{6,}\d(?![\w.:-])")
_NAMED = re.compile(r"""(\b(?:name|company|caller|title)\s*[=:]\s*)(['"]).*?\2""", re.IGNORECASE)


def mask_log(text: str) -> str:
    """Phone numbers (8+ digits, optional + and spaces) -> [szám …67] (last two digits kept, so two lines can still be matched), name='...' /
    company='...' values -> '***'. Times, call ids, versions and error names stay readable."""
    def phone(m):
        digits = re.sub(r"\D", "", m.group(0))
        return f"[szám …{digits[-2:]}]" if len(digits) >= 8 else m.group(0)   # ids / extensions stay
    text = _NAMED.sub(lambda m: f"{m.group(1)}{m.group(2)}***{m.group(2)}", text)
    return _PHONE.sub(phone, text)


def log_tail(path: Path, lines: int = LOG_LINES) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            tail = fh.readlines()[-lines:]
    except OSError as e:
        return f"(a napló nem olvasható: {e.__class__.__name__})"
    text = mask_log("".join(tail))
    return text[-LOG_MAX_CHARS:]


def endpoint(cfg: dict | None) -> str:
    over = str(((cfg or {}).get("feedback") or {}).get("endpoint") or "").strip()
    return over or FEEDBACK_ENDPOINT


class _Sender(QObject):
    done = Signal(bool, str)          # ok, message (Hungarian, for the dialog)

    def send(self, url: str, payload: dict, timeout: float = 15):
        def run():
            try:
                import requests
                r = requests.post(url, json=payload, timeout=timeout,
                                  headers={"Accept": "application/json", "User-Agent": "CallCompanion"})
                ok = r.status_code == 200
                if not ok:
                    log.warning("feedback: HTTP %s %s", r.status_code, r.text[:200].replace("\n", " "))
                self.done.emit(ok, "" if ok else f"A küldés nem sikerült (HTTP {r.status_code}).")
            except Exception as e:                       # network, TLS, proxy...
                log.warning("feedback: send failed: %s", e.__class__.__name__)
                self.done.emit(False, "A küldés nem sikerült (nincs kapcsolat a szerverrel).")
        threading.Thread(target=run, daemon=True, name="feedback").start()


class FeedbackDialog(QDialog):
    def __init__(self, version: str, report_text: str, log_path: Path, cfg: dict | None = None, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle("Visszajelzés – Call Companion")
        self.setMinimumSize(460, 470)
        self.setStyleSheet(theme.qss_main() + self._qss())
        self._version, self._report, self._log_path = version, report_text, Path(log_path)
        self._url = endpoint(cfg)
        self._kind = "bug"
        self._sender = _Sender(self)
        self._sender.done.connect(self._sent)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)
        title = QLabel("Visszajelzés")
        title.setStyleSheet("font-size: 17px; font-weight: 700;")
        root.addWidget(title)
        hint = QLabel("Írd le, mi történt vagy mit szeretnél. Az üzenet a program készítőjéhez megy.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        root.addWidget(hint)

        kinds = QHBoxLayout()
        kinds.setSpacing(2)
        group = QButtonGroup(self)
        for key, label in KINDS:
            b = QPushButton(label, objectName="seg")
            b.setCheckable(True)
            b.setChecked(key == self._kind)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: self._set_kind(k))
            group.addButton(b)
            kinds.addWidget(b)
        kinds.addStretch(1)
        root.addLayout(kinds)

        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("Pl.: 10:42-kor bejövő hívásnál nem jelent meg a kártya…")
        self.text.textChanged.connect(self._update_send)
        root.addWidget(self.text, 1)
        self.email = QLineEdit()
        self.email.setPlaceholderText("E-mail címed a válaszhoz (nem kötelező)")
        root.addWidget(self.email)
        self.attach = QCheckBox("Napló csatolása (telefonszámok és nevek nélkül)")
        self.attach.setChecked(True)
        root.addWidget(self.attach)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setVisible(False)
        root.addWidget(self.status)
        foot = QHBoxLayout()
        foot.addStretch(1)
        cancel = QPushButton("Mégse")
        cancel.clicked.connect(self.reject)
        foot.addWidget(cancel)
        self.send_btn = QPushButton("Küldés", objectName="primary")
        self.send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_btn.clicked.connect(self._send)
        foot.addWidget(self.send_btn)
        root.addLayout(foot)
        self._update_send()

    @staticmethod
    def _qss() -> str:
        p = theme.palette()
        return (f"QPlainTextEdit {{ background: {p.mantle if not p.light else '#ffffff'}; "
                f"border: 1px solid {p.surface2}; border-radius: 8px; padding: 6px 8px; }}"
                f"QPlainTextEdit:focus {{ border-color: {p.blue}; }}")

    def _set_kind(self, key: str):
        self._kind = key
        self.attach.setChecked(key == "bug")

    def _update_send(self):
        self.send_btn.setEnabled(bool(self.text.toPlainText().strip()))

    def _compose(self, with_log: bool) -> tuple[str, str]:
        kind = dict(KINDS)[self._kind]
        subject = f"Call Companion {self._version} – {kind}"
        parts = [self.text.toPlainText().strip(), "", "—" * 20,
                 f"Típus: {kind}", f"Verzió: {self._version}",
                 f"Windows {platform.release()} {platform.version()}",
                 f"Idő: {datetime.now():%Y-%m-%d %H:%M}", "", self._report]
        if with_log:
            parts += ["", "—" * 8 + " app.log (vége, maszkolva) " + "—" * 8, log_tail(self._log_path)]
        return subject, "\n".join(parts)

    def _status(self, text: str, tone: str | None):
        theme.set_tone(self.status, tone)
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def _send(self):
        subject, body = self._compose(self.attach.isChecked())
        if not self._url:
            self._fallback(subject, body, "")
            return
        self.send_btn.setEnabled(False)
        self._status("Küldés…", "subtext")
        payload = {"_subject": subject, "message": body, "kind": self._kind, "version": self._version}
        reply = self.email.text().strip()
        if reply:
            payload["email"] = reply                 # Formspree uses it as Reply-To
        log.info("feedback: sending (%s, %d chars, log %s)", self._kind, len(body),
                 "yes" if self.attach.isChecked() else "no")
        self._sender.send(self._url, payload)

    def _sent(self, ok: bool, message: str):
        if not _alive(self):
            return
        if ok:
            log.info("feedback: sent")
            self._status("Köszönöm, megkaptam! ✓", "green")
            self.send_btn.setText("Elküldve")
            self.send_btn.setEnabled(False)
            self.text.setReadOnly(True)
            return
        subject, body = self._compose(self.attach.isChecked())
        self._fallback(subject, body, message)

    def _fallback(self, subject: str, body: str, why: str):
        """Clipboard + e-mail program. The mail body is short (mailto has a length limit): the full text is pasted."""
        QApplication.clipboard().setText(f"{subject}\n\n{body}")
        short = (self.text.toPlainText().strip()[:600] +
                 "\n\n(A teljes szöveg a vágólapon van: illeszd be ide, Ctrl+V.)")
        QDesktopServices.openUrl(QUrl(f"mailto:{FEEDBACK_EMAIL}?subject={quote(subject)}&body={quote(short)}"))
        self._status(((why + " ") if why else "") +
                     f"A szöveg a vágólapra került, és megnyílt a levelezőprogram ({FEEDBACK_EMAIL}): "
                     "illeszd be a levélbe (Ctrl+V), és küldd el.", "yellow" if why else "subtext")
        self.send_btn.setEnabled(True)
        log.info("feedback: fallback to clipboard + mailto (%s)", why or "no endpoint")


def _alive(w) -> bool:
    try:
        from shiboken6 import isValid
        return isValid(w)
    except Exception:
        return True
