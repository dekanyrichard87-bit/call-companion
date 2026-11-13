"""Call Companion - entry point (tray app, controller, autostart helper)."""
import argparse
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QLockFile, QObject, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon
from shiboken6 import isValid   # ships with PySide6: False once the C++ side of a widget is deleted

import config
import updater
from bria_api import BriaApi, BriaApiError
from crm import CallerInfo, MiniCrmClient
from detectors import BriaLogDetector
from dev_window import DevWindow
from settings_dialog import SettingsDialog
from events import CallEvent, normalize_number
from history import HistoryStore
from local_api import LocalApi
from overlay import CallOverlay
from settings import Settings
from status_window import Health, MainWindow, StatusModel, attach_to_tray
from version import __version__

log = logging.getLogger("callcompanion")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def set_autostart(enable: bool):
    """Per-user Run registry entry - for a source checkout only. The installer (installer.iss) uses a Startup-folder
    shortcut instead and removes this entry, so an installed copy is never started twice."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enable:
            if getattr(sys, "frozen", False):
                cmd = f'"{sys.executable}"'
            else:
                pyw = Path(sys.executable).with_name("pythonw.exe")
                cmd = f'"{pyw}" "{Path(__file__).resolve()}"'
            winreg.SetValueEx(key, config.APP_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, config.APP_NAME)
            except FileNotFoundError:
                pass


def make_icon() -> QIcon:
    ico = config.resource_path("assets/app.ico")          # bundled by callcompanion.spec
    if ico.exists():
        icon = QIcon(str(ico))
        if not icon.isNull():
            return icon
    pm = QPixmap(64, 64)                                   # fallback: plain dot (should not happen)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#89b4fa"))
    p.setPen(QColor("#1e1e2e"))
    p.drawEllipse(6, 6, 52, 52)
    p.end()
    return QIcon(pm)


class Controller(QObject):
    event_sig = Signal(object)          # CallEvent (from detector / local API threads)
    lookup_sig = Signal(str, object)    # call_id, CallerInfo
    session_sig = Signal(object, str)   # True / False / None, message
    ext_sig = Signal(object)            # payload dict pushed by the Chrome extension
    status_sig = Signal(str)            # tray tooltip text
    tray_msg_sig = Signal(str, str)     # tray balloon title, text
    company_sig = Signal(str, str)      # call_id, company name (stage 2: arrives after the name)
    bria_sig = Signal(str, str, bool, str, object)   # v0.8.0: call_id, action, ok, message, extra
    bria_check_sig = Signal(bool, str)                # v0.8.0: developer window "Bria vezérlés ellenőrzése"
    bria_caps_sig = Signal()                          # v0.9.0: the transfer capability check finished

    def __init__(self, cfg: dict, store: HistoryStore | None = None, settings: Settings | None = None):
        super().__init__()
        self.cfg = cfg
        self.settings = settings
        self.bria_api = BriaApi(cfg.get("bria_api") or {}, f"CallCompanion/{__version__}")
        self.mock = cfg["minicrm"]["mock"]
        self.source = cfg["minicrm"]["cookie_source"]
        self.client = MiniCrmClient(cfg["minicrm"])
        self.pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="crm")
        self.overlay: CallOverlay | None = None
        self.current_id: str | None = None
        self.missed: dict[str, CallOverlay] = {}   # call id -> missed-call card (stays until the user closes it)
        self._calls: dict[str, dict] = {}          # call id -> {"dir", "answered", "when"} while the call is live
        self.services = []
        self.ext_seen = False
        self.session_state = None
        self.api_service = None
        self.bria_service = None
        self.ext_last = None            # time.time() of the last extension push
        self.bria_last_event = ""       # HH:MM:SS of the last call event that came from Bria
        self.status = StatusModel(store)    # everything the windows show; the call list is persistent (v0.6.0)
        self.health_timer = QTimer(self)
        self.health_timer.setInterval(10_000)
        self.health_timer.timeout.connect(self.refresh_health)
        self.event_sig.connect(self.on_event)
        self.lookup_sig.connect(self.on_lookup)
        self.session_sig.connect(self.on_session)
        self.ext_sig.connect(self.on_ext)
        self.company_sig.connect(self.on_company)
        self.bria_sig.connect(self.on_bria_result)
        self.bria_caps_sig.connect(self._apply_call_controls)
        if settings is not None:
            settings.changed.connect(self._extensions_changed)

    # ---- lifecycle ---------------------------------------------------------------------
    def is_busy(self) -> bool:
        """True while a call is ringing / running or a missed-call card is still on screen.
        The auto-updater waits for this to become False before it restarts the app."""
        if self.current_id is not None or self._calls:
            return True
        return any(isValid(card) for card in self.missed.values())

    def start_services(self):
        la = self.cfg["local_api"]
        if la["enabled"]:
            self.api_service = LocalApi(la, self.event_sig.emit, self.ext_sig.emit)
            self.services.append(self.api_service)
        bria = self.cfg["detectors"]["bria_log"]
        if bria["enabled"]:
            self.bria_service = BriaLogDetector(bria, self.event_sig.emit)
            self.services.append(self.bria_service)
        for s in self.services:
            s.start()
        if not self.mock and self.source == "extension":
            QTimer.singleShot(30_000, self._watchdog)
        self.client.start_warmup()            # keeps one MiniCRM connection warm (no-op without a session)
        self.health_timer.start()
        QTimer.singleShot(1_500, self.refresh_health)
        if self.bria_api.enabled:
            QTimer.singleShot(20_000, self._maybe_check_transfer)   # v0.9.0: is ↪ Átkapcsolás possible here?

    def stop(self):
        self.health_timer.stop()
        self.client.stop_warmup()
        for s in self.services:
            s.stop()
        self.pool.shutdown(wait=False, cancel_futures=True)

    # ---- MiniCRM session ---------------------------------------------------------------
    def check_session(self):
        if self.mock:
            self.session_sig.emit(True, "mock")
            return

        def job():
            try:
                if self.source != "extension":
                    log.info("Cookies loaded from %s", self.client.load_cookies())
                elif not self.client.has_session:
                    # extension reached us but there is no MiniCRM cookie -> not logged in;
                    # extension not seen yet -> unknown (still waiting)
                    self.session_sig.emit(False if self.ext_seen else None, "no cookies")
                    return
                self.session_sig.emit(self.client.session_ok(), "")
            except Exception as e:
                log.warning("session problem: %s", e)
                self.session_sig.emit(False, str(e))
        self.pool.submit(job)

    def on_ext(self, payload: dict):
        raw = payload.get("cookies")
        raw = raw if isinstance(raw, list) else []     # a malformed push must not raise in a slot
        cookies = [c for c in raw
                   if isinstance(c, dict) and isinstance(c.get("name"), str)
                   and isinstance(c.get("value"), str)]
        ua = payload.get("userAgent")
        self.ext_seen = True
        self.ext_last = time.time()
        self.status.set_extension(Health.OK, f"Utolsó szinkron {time.strftime('%H:%M:%S')} "
                                             f"({len(cookies)} süti)")
        changed = self.client.set_extension_session(cookies, ua if isinstance(ua, str) else "")
        log.info("extension push (%s): %d cookies, changed=%s", payload.get("reason"),
                 len(cookies), changed)
        if changed or self.session_state is None:
            self.check_session()

    def on_session(self, ok, msg: str):
        log.info("session state: %s %s", ok, msg)
        prev, self.session_state = self.session_state, ok
        if self.mock:
            text = "MiniCRM: teszt (mock) mód"
        else:
            text = {True: "aktív", False: "jelentkezzen be a MiniCRM-be",
                    None: "várakozás a Chrome-ra"}[ok if ok in (True, False) else None]
        self.status_sig.emit("Call Companion – " + text)
        if self.mock:
            self.status.set_minicrm(Health.OK, "Mock mód (nincs valódi keresés)")
        elif ok is True:
            self.status.set_minicrm(Health.OK, "Bejelentkezve, a munkamenet érvényes")
            self.client.kick_warmup()
        elif ok is False:
            self.status.set_minicrm(Health.ERROR, "Jelentkezzen be a MiniCRM-be a Chrome-ban"
                                    + (f" ({msg})" if msg else ""))
        elif self.ext_seen:
            self.status.set_minicrm(Health.WARN, "A munkamenet nem ellenőrizhető (hálózat?)")
        else:
            self.status.set_minicrm(Health.WARN, "Várakozás a Chrome bővítményre")
        if ok is False and prev is not False:
            self._show_notice("Jelentkezzen be a MiniCRM-be",
                              "Nyissa meg a Chrome-ot és lépjen be a MiniCRM-be, hogy a hívók neve megjelenjen.")
        # Tell the status window a session check has completed (drives the re-check button feedback).
        self.status.recheck_finished.emit()

    def _watchdog(self):
        if not self.ext_seen:
            self.tray_msg_sig.emit("Call Companion",
                                   "Nincs kapcsolat a Chrome-mal. Nyissa meg a Chrome-ot (benne a MiniCRM-mel), "
                                   "hogy a hívók neve megjelenjen.")

    # ---- status window health ------------------------------------------------------------
    def refresh_health(self):
        self._health_bria()
        self._health_api()
        self._health_extension()

    def _bria_log_path(self):
        """Best effort: the log file the detector tails (detector attribute or config key)."""
        bria = self.cfg["detectors"]["bria_log"]
        for key in ("path", "log_path", "log_file", "file"):
            for v in (getattr(self.bria_service, key, None), bria.get(key)):
                if v:
                    return Path(os.path.expandvars(str(v)))
        return None

    def _health_bria(self):
        if not self.cfg["detectors"]["bria_log"]["enabled"]:
            self.status.set_bria(Health.WARN, "Kikapcsolva (config.json)")
            return
        det = self.bria_service
        if det is None or not getattr(det, "is_alive", lambda: True)():
            self.status.set_bria(Health.ERROR, "A naplófigyelő nem fut")
            return
        last = f" • utolsó hívásesemény {self.bria_last_event}" if self.bria_last_event else ""
        if getattr(det, "path", None) is None:     # thread runs but log_globs matched no file
            self.status.set_bria(Health.WARN, "Nem található Bria naplófájl (log_globs?)" + last)
            return
        path = self._bria_log_path()
        if path is None:
            self.status.set_bria(Health.OK, "Naplófigyelés aktív" + last)
            return
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            self.status.set_bria(Health.ERROR, f"Nem található: {path}")
            return
        if age > 1800:
            self.status.set_bria(Health.WARN, f"A napló {int(age // 60)} perce nem változott "
                                              f"– fut a Bria?{last}")
        else:
            self.status.set_bria(Health.OK, "Log.txt figyelése" + last)

    def _health_api(self):
        la = self.cfg["local_api"]
        if self.api_service is None:
            self.status.set_api(Health.WARN, "Kikapcsolva (config.json)")
        elif self.api_service.httpd is None:
            self.status.set_api(Health.ERROR, f"Nem indult el: {la['host']}:{la['port']} (foglalt?)")
        else:
            self.status.set_api(Health.OK, f"{la['host']}:{la['port']}")

    def _health_extension(self):
        if self.source != "extension":
            self.status.set_extension(Health.UNKNOWN, f"Nem használt (cookie_source: {self.source})")
        elif self.ext_last is None:
            self.status.set_extension(Health.UNKNOWN, "Várakozás az első szinkronra…")
        elif time.time() - self.ext_last > 12 * 60:      # the extension pushes every 5 minutes
            mins = int((time.time() - self.ext_last) // 60)
            self.status.set_extension(Health.WARN, f"Nincs szinkron {mins} perce (Chrome zárva?)")

    # ---- overlay -----------------------------------------------------------------------
    def _ui_kwargs(self):
        ui = self.cfg["ui"]
        return {"margin": ui["margin_px"], "answered_fade_ms": ui["answered_fade_ms"],
                "close_missed_on_action": bool(ui.get("missed_close_on_action", True)),
                "end_linger_ms": int(ui.get("end_linger_ms", 3000))}

    # CallOverlay deletes itself (deleteLater) after it has faded out, so self.overlay can point at a
    # dead Qt object. Never call methods on it directly: go through _live_overlay() / _drop_overlay().
    def _live_overlay(self):
        """The current overlay, or None. Forgets a reference whose Qt object is already gone."""
        ov = self.overlay
        if ov is not None and not isValid(ov):
            self.overlay = ov = None
        return ov

    def _new_overlay(self) -> CallOverlay:
        ov = CallOverlay(**self._ui_kwargs())
        ov.closed.connect(lambda ov=ov: self._on_overlay_closed(ov))
        cid = self.current_id
        if cid:                  # a call card (not a notice): call control through the Bria Desktop API
            exts = self.settings.extensions if self.settings is not None else []
            ov.set_call_controls(self.bria_api.enabled, exts, bool(self.bria_api.transfer_known()))
            ov.answer_requested.connect(lambda cid=cid, ov=ov: self._bria_action(cid, ov, "answer", None))
            ov.end_requested.connect(lambda kind, cid=cid, ov=ov: self._bria_action(cid, ov, "end", kind))
            ov.hold_requested.connect(lambda hold, cid=cid, ov=ov: self._bria_action(cid, ov, "hold", hold))
            ov.transfer_requested.connect(
                lambda number, name, cid=cid, ov=ov: self._bria_action(cid, ov, "transfer", (number, name)))
        self.overlay = ov
        return ov

    def _on_overlay_closed(self, ov):
        if self.overlay is ov:   # a late signal from an overlay that was already replaced is ignored
            self.overlay = None
            cid = self.current_id
            if cid and (self._calls.get(cid) or {}).get("transferred"):
                # the transferred call has left this phone; do not wait for a hang-up that may never be logged
                self._calls.pop(cid, None)
                self.current_id = None
        for cid, card in list(self.missed.items()):
            if card is ov:
                del self.missed[cid]

    def _overlay_for(self, call_id: str):
        """The card that belongs to this call: the live card, or the missed-call card it turned into."""
        if call_id == self.current_id:
            return self._live_overlay()
        ov = self.missed.get(call_id)
        if ov is not None and not isValid(ov):
            self.missed.pop(call_id, None)
            return None
        return ov

    def _keep_missed(self, call_id: str, ov: CallOverlay, when: str):
        """Turn the ringing card into a missed-call card that stays on screen until the user closes it.
        A repeated missed call from the same number replaces the older card and raises its counter."""
        count = 1
        for cid, old in list(self.missed.items()):
            if not isValid(old):
                del self.missed[cid]
            elif ov.number and old.number == ov.number:
                count += old.missed_count
                del self.missed[cid]
                old.end()
        self.overlay = None
        self.missed[call_id] = ov
        ov.set_missed(count, when)
        limit = max(1, int(self.cfg["ui"].get("missed_max", 4)))     # the stack must fit on the screen
        while len(self.missed) > limit:
            old = self.missed.pop(next(iter(self.missed)))
            if isValid(old):
                old.end()

    END_TEXT = {None: "HÍVÁS BEFEJEZVE", "rejected": "ELUTASÍTVA", "elsewhere": "MÁSHOL FOGADTÁK",
                "unanswered": "NEM VETTÉK FEL", "missed": "NEM FOGADOTT HÍVÁS"}

    @staticmethod
    def _outcome(meta: dict, ev: CallEvent):
        """How a call that was never answered ended: missed / rejected / elsewhere / unanswered (we dialled)."""
        if meta.get("answered"):
            return None
        if meta.get("dir") == "out":
            return "unanswered"
        return {"rejected": "rejected", "failed": "rejected", "elsewhere": "elsewhere"}.get(ev.reason, "missed")

    def _drop_overlay(self):
        ov, self.overlay = self.overlay, None
        if ov is not None and isValid(ov):
            try:
                ov.hide()
                ov.deleteLater()
            except RuntimeError:     # deleted between the isValid check and the call
                pass

    def _show_notice(self, title, text):
        if self.current_id:      # never cover a live call card with a notice
            return
        self._drop_overlay()
        self._new_overlay().show_notice(title, text)

    # ---- call events -------------------------------------------------------------------
    def on_event(self, ev: CallEvent):
        tags = [ev.source] + (["out"] if ev.direction == "out" else []) + ([ev.reason] if ev.reason else [])
        log.info("event: %s %s %s (%s)", ev.kind, ev.number, ev.call_id, ", ".join(tags))
        if ev.source not in ("sim", "http"):
            self.bria_last_event = time.strftime("%H:%M:%S")
        outcome, meta = None, self._calls.get(ev.call_id) or {}
        if ev.kind == "answered":
            if not meta.get("answered"):
                meta["answered_at"] = time.monotonic()
            meta["answered"] = True
            self.status.call_answered(ev.call_id)
        elif ev.kind == "ended":
            meta = self._calls.pop(ev.call_id, None) or {"dir": ev.direction}
            outcome = self._outcome(meta, ev)
            if meta.get("user_end") == "reject" and outcome == "missed":
                # declined with ✕ Elutasít: whatever end reason Bria logged, it is not a missed call (unverified
                # which reason Bria writes for an API decline; this line shows it in the live test)
                log.info("call %s declined from the card (log reason %s)", ev.call_id, ev.reason)
                outcome = "rejected"
            talk = int(time.monotonic() - meta["answered_at"]) if meta.get("answered_at") else 0
            self.status.call_ended(ev.call_id, outcome=outcome, duration_s=talk)
        if ev.kind == "ringing":
            self._calls[ev.call_id] = {"dir": ev.direction, "answered": False, "when": time.strftime("%H:%M")}
            while len(self._calls) > 50:                 # calls whose end was never seen
                self._calls.pop(next(iter(self._calls)))
            self.status.call_ringing(ev.call_id, ev.number, ev.direction)
            self._drop_overlay()
            self.current_id = ev.call_id
            self._new_overlay().show_ringing(ev.number, ev.direction)    # immediate: number only
            fut = self.pool.submit(self._safe_lookup, ev.number)
            fut.add_done_callback(lambda f, cid=ev.call_id: self.lookup_sig.emit(cid, f.result()))
            self._maybe_check_transfer()
        elif ev.call_id == self.current_id:
            ov = self._live_overlay()
            if ev.kind == "answered":
                if ov:
                    ov.set_answered()
            elif ev.kind == "ended":
                if ov:
                    if outcome == "missed" and self.cfg["ui"].get("missed_persistent", True):
                        self._keep_missed(ev.call_id, ov, meta.get("when", ""))
                    else:
                        ov.finish(self.END_TEXT.get(outcome, "HÍVÁS BEFEJEZVE"))     # lingers, then fades
                self.current_id = None   # reset even if the card already faded out

    def _safe_lookup(self, number):
        try:
            return self.client.lookup(number)
        except Exception as e:
            log.exception("lookup crashed")
            return CallerInfo(error=e.__class__.__name__)

    def on_lookup(self, call_id: str, info):
        state = "found" if info.found else ("failed" if info.error else "unknown")
        self.status.call_resolved(call_id, info.name if info.found else None,
                                  info.profile_url or None, state, info.error, info.elapsed_ms)
        if info.found and info.company:
            self.status.call_company(call_id, info.company)
        ov = self._overlay_for(call_id)
        if ov:
            ov.set_caller(info)
        # Stage 2: the name is already on the card. Fetch the company in the background (optional, slower);
        # it pops in on the second line when it arrives. Since v0.6.0 it is fetched even when the card is already
        # gone, so the call history gets the company too (cached per contact for 12 h).
        if info.found and not info.company and self.client.company_enabled():
            try:
                self.pool.submit(self._company_job, call_id, info)
            except RuntimeError:            # pool already shut down (app is quitting)
                pass

    def _company_job(self, call_id: str, info):
        try:
            company = self.client.fetch_company(info)
        except Exception:
            log.exception("company fetch crashed")
            return
        if company:
            self.company_sig.emit(call_id, company)

    def on_company(self, call_id: str, company: str):
        self.status.call_company(call_id, company)
        ov = self._overlay_for(call_id)
        if ov:
            ov.set_company(company)

    # ---- v0.8.0: hold / transfer through the Bria Desktop API ------------------------------------------------
    def _extensions_changed(self):
        self._apply_call_controls()

    def _apply_call_controls(self):
        ov = self._live_overlay()
        if ov is not None and self.current_id:
            exts = self.settings.extensions if self.settings is not None else []
            ov.set_call_controls(self.bria_api.enabled, exts, bool(self.bria_api.transfer_known()))

    def _maybe_check_transfer(self):
        """v0.9.0: does this Bria offer transferCall? Checked in the background at start, when a call rings
        (if unknown or older than 30 min) and by the developer check; the card shows ↪ Átkapcsolás only if yes."""
        if not self.bria_api.transfer_check_due():
            return

        def job():
            try:
                self.bria_api.check_transfer()
            except BriaApiError as e:
                log.info("bria api: transfer capability not checked: %s", e.code)
            except Exception:
                log.exception("transfer capability check crashed")
            self.bria_caps_sig.emit()
        try:
            self.pool.submit(job)
        except RuntimeError:
            pass

    def _bria_action(self, call_id: str, ov, action: str, arg):
        number = ov.number if isValid(ov) else ""
        log.info("bria action %s%s for call %s", action, f" ({arg})" if action == "end" else "", call_id)
        meta = self._calls.get(call_id)
        if action == "end" and meta is not None:
            meta["user_end"] = arg          # set before the request: Bria's log line may arrive before its answer

        def job():
            try:
                if action == "answer":
                    self.bria_api.answer(number)
                    self.bria_sig.emit(call_id, "answer", True, "", None)
                elif action == "end":
                    self.bria_api.end_call(number)
                    self.bria_sig.emit(call_id, "end", True, "", arg)
                elif action == "hold":
                    held = self.bria_api.set_hold(number, bool(arg))
                    self.bria_sig.emit(call_id, "hold", True, "", held)
                else:
                    ext, name = arg
                    self.bria_api.transfer(number, ext)
                    self.bria_sig.emit(call_id, "transfer", True, "", (ext, name))
            except BriaApiError as e:
                log.warning("bria action %s failed: %s", action, e)
                self.bria_sig.emit(call_id, action, False, e.user_text, e.code)
            except Exception as e:                       # never leave the card buttons disabled
                log.exception("bria action %s crashed", action)
                self.bria_sig.emit(call_id, action, False, "Váratlan hiba a Bria vezérlésben", e.__class__.__name__)
        try:
            self.pool.submit(job)
        except RuntimeError:
            pass

    BRIA_LINK_ERRORS = ("BRIA-API-OFF", "BRIA-API-TIMEOUT", "BRIA-API-HANDSHAKE", "BRIA-API-CLOSED")

    def on_bria_result(self, call_id: str, action: str, ok: bool, message: str, extra):
        now = time.strftime("%H:%M:%S")
        if ok:
            self.status.set_bria_api(Health.OK, f"Rendben • utolsó művelet {now} ({action})")
        elif extra in self.BRIA_LINK_ERRORS:
            self.status.set_bria_api(Health.ERROR, f"{extra}: {message} ({now})")
        else:
            self.status.set_bria_api(Health.WARN, f"{extra}: {message} ({now})")
        ov = self._overlay_for(call_id)
        if not ok:
            meta = self._calls.get(call_id)
            if action == "end" and meta is not None:
                meta.pop("user_end", None)
            if ov:
                ov.control_failed(message)
            return
        # answer / end: the card changes when Bria's log reports the answer / the end (on_event)
        if action == "hold":
            self.status.call_hold(call_id, bool(extra))
            if ov:
                ov.set_hold(bool(extra))
        elif action == "transfer":
            ext, name = extra
            meta = self._calls.get(call_id)
            if meta is not None:
                meta["transferred"] = True
            self.status.call_note(call_id, f"Átkapcsolva: {name} ({ext})", "transferred")
            if ov:
                ov.transferred(name, ext)

    def check_bria(self):
        def job():
            try:
                text = self.bria_api.check()
                self.bria_check_sig.emit(True, text)
                self.bria_caps_sig.emit()
            except BriaApiError as e:
                self.bria_check_sig.emit(False, f"{e.code}: {e.user_text} ({e.detail})")
            except Exception as e:
                log.exception("bria check crashed")
                self.bria_check_sig.emit(False, f"Váratlan hiba: {e.__class__.__name__}")
        self.pool.submit(job)

    def on_bria_check(self, ok: bool, text: str):
        if ok:
            self.status.set_bria_api(Health.OK, text)
        else:
            self.status.set_bria_api(Health.ERROR if any(c in text for c in self.BRIA_LINK_ERRORS)
                                     else Health.WARN, text)


SIM_SCENARIOS = {                      # (delay in ms, event kind)
    "answered": [(0, "ringing"), (4000, "answered"), (24000, "ended")],      # 20 s of talk: the counter is visible
    "missed":   [(0, "ringing"), (6000, "ended")],
    "long":     [(0, "ringing"), (15000, "ended")],
    "outgoing": [(0, "ringing"), (4000, "answered"), (20000, "ended")],      # we dial, they answer after 4 s
    "outgoing_unanswered": [(0, "ringing"), (8000, "ended")],
}


def simulate(ctl: Controller, raw_number: str, scenario: str = "answered"):
    n = normalize_number(raw_number)
    cid = f"sim-{n}-{int(time.time() * 1000)}"       # unique per run, so repeats are separate log rows
    direction = "out" if scenario.startswith("outgoing") else "in"
    answered = False
    for delay, kind in SIM_SCENARIOS.get(scenario, SIM_SCENARIOS["answered"]):
        answered = answered or kind == "answered"
        reason = ("bye" if answered else "cancel") if kind == "ended" else ""
        QTimer.singleShot(delay, lambda kind=kind, reason=reason: ctl.event_sig.emit(
            CallEvent(kind, cid, n, "sim", direction, reason)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--simulate", metavar="NUMBER", help="fake a call: ring -> answer -> end")
    ap.add_argument("--probe-bria", nargs="?", const=90, type=int, metavar="SECONDS",
                    help="capture Bria log lines during a test call into bria_probe.txt")
    ap.add_argument("--show", action="store_true", help="open the status window at startup")
    ap.add_argument("--version", action="store_true", help="print the version and exit")
    ap.add_argument("--install-autostart", action="store_true")
    ap.add_argument("--remove-autostart", action="store_true")
    ap.add_argument("--check-update", action="store_true",
                    help="query GitHub Releases, print the result to app.log / console and exit")
    ap.add_argument("--after-install", action="store_true",
                    help="started by the installer: show a short tray message")
    args = ap.parse_args()

    if args.version:
        print(f"Call Companion {__version__}")
        return

    if args.install_autostart or args.remove_autostart:
        set_autostart(args.install_autostart)
        print("Autostart", "enabled" if args.install_autostart else "removed")
        return

    if args.probe_bria:
        import probe
        probe.run(config.load(), args.probe_bria)
        return

    handlers = [logging.FileHandler(config.data_dir() / "app.log", encoding="utf-8")]
    if sys.stderr:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.check_update:
        result = updater.check_once(config.load())
        log.info("%s", result)
        print(result)
        return

    log.info("Call Companion %s starting", __version__)
    lock = QLockFile(str(config.data_dir() / "app.lock"))   # single instance (autostart + manual start)
    if not lock.tryLock(200):
        log.info("Another instance is already running - exiting.")
        return

    cfg = config.load()
    if cfg["minicrm"]["mock"]:
        log.warning("MiniCRM MOCK mode is ON (config.json -> minicrm.mock). No real lookups.")

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    icon = make_icon()
    app.setWindowIcon(icon)
    hist_cfg = cfg.get("history") or {}
    store = HistoryStore(config.data_dir() / "history.db", hist_cfg.get("retention_days", 90),
                         hist_cfg.get("max_rows", 2000))
    settings = Settings(config.data_dir() / "settings.json")
    ctl = Controller(cfg, store, settings)
    data_dir = str(config.data_dir())
    win = MainWindow(ctl.status, __version__, settings.dev_mode, data_dir)
    dev = DevWindow(ctl.status, data_dir, __version__)
    dev.recheck_requested.connect(ctl.check_session)
    dev.simulate_requested.connect(lambda number, scenario: simulate(ctl, number, scenario))
    dev.bria_api_check.connect(ctl.check_bria)
    ctl.bria_check_sig.connect(ctl.on_bria_check)
    ctl.bria_check_sig.connect(dev.bria_check_result)

    def quit_app():
        win.prepare_quit()
        dev.prepare_quit()
        ctl.stop()
        store.close()
        app.quit()

    updates = updater.UpdateManager(cfg, is_busy=ctl.is_busy, quit_app=quit_app, parent=app)
    dev.check_updates.connect(lambda: updates.check(manual=True))

    tray = QSystemTrayIcon(icon, app)
    tray.setToolTip("Call Companion")
    menu = QMenu()
    menu.setToolTipsVisible(True)

    def build_menu():
        """Normal mode: Status, Call history, Quit. Developer mode adds the technical entries."""
        menu.clear()
        st = menu.addAction("Állapot")
        st.triggered.connect(lambda: win.show_and_raise())
        bold = st.font()
        bold.setBold(True)
        st.setFont(bold)
        menu.addAction("Híváselőzmények").triggered.connect(lambda: win.show_and_raise(history=True))
        if settings.dev_mode:
            menu.addSeparator()
            dv = menu.addAction(f"Fejlesztői eszközök (v{__version__})")
            dv.triggered.connect(dev.show_and_raise)
            menu.addAction("Teszthívás – bejövő").triggered.connect(lambda: simulate(ctl, "+36301234567"))
            menu.addAction("Teszthívás – kimenő").triggered.connect(
                lambda: simulate(ctl, "+36301234567", "outgoing"))
            menu.addAction("MiniCRM újraellenőrzése").triggered.connect(ctl.check_session)
            upd = menu.addAction("Frissítések keresése")
            upd.triggered.connect(lambda: updates.check(manual=True))
            upd.setToolTip(updates.state_text)
        menu.addSeparator()
        menu.addAction("Kilépés").triggered.connect(quit_app)

    def set_dev_mode(on: bool):
        settings.set_dev_mode(on)
        win.set_dev_mode(on)
        if not on:
            dev.hide()
        build_menu()

    def open_settings():
        SettingsDialog(settings, __version__, update_state=lambda: updates.state_text,
                       check_updates=lambda: updates.check(manual=True), set_dev_mode=set_dev_mode,
                       open_dev_tools=dev.show_and_raise, parent=win).exec()

    win.open_settings.connect(open_settings)              # v0.9.0: ⚙ gear in the main window
    menu.aboutToShow.connect(build_menu)              # also refreshes the update tooltip
    build_menu()
    tray.setContextMenu(menu)
    ctl.status_sig.connect(tray.setToolTip)
    ctl.tray_msg_sig.connect(lambda t, m: tray.showMessage(
        t, m, QSystemTrayIcon.MessageIcon.Warning, 8000))
    updates.message.connect(lambda t, m: tray.showMessage(
        t, m, QSystemTrayIcon.MessageIcon.Information, 6000))
    attach_to_tray(tray, win)             # one click on the tray icon -> main window
    tray.show()

    ctl.start_services()
    ctl.check_session()
    updates.start()
    if args.after_install:
        QTimer.singleShot(1_500, lambda: tray.showMessage(
            "Call Companion", f"A {__version__} verzió fut. Kattintson a tálcaikonra az állapothoz és a hívásokhoz.",
            QSystemTrayIcon.MessageIcon.Information, 6000))
    if args.show:
        QTimer.singleShot(300, win.show_and_raise)
    if args.simulate:
        QTimer.singleShot(600, lambda: simulate(ctl, args.simulate))
    code = app.exec()
    lock.unlock()
    sys.exit(code)


if __name__ == "__main__":
    main()
