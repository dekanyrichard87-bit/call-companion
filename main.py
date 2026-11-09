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
from crm import CallerInfo, MiniCrmClient
from detectors import BriaLogDetector
from events import CallEvent, normalize_number
from local_api import LocalApi
from overlay import CallOverlay
from status_window import Health, StatusModel, StatusWindow, attach_to_tray
from version import __version__

log = logging.getLogger("callcompanion")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def set_autostart(enable: bool):
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
    pm = QPixmap(64, 64)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#89b4fa"))
    p.setPen(QColor("#1e1e2e"))
    p.drawEllipse(6, 6, 52, 52)
    p.end()
    return QIcon(pm)


def _clean_user_name(value) -> str | None:
    """Display name pushed by the extension: plain text, 2-60 chars, no control characters."""
    if not isinstance(value, str):
        return None
    value = " ".join("".join(ch for ch in value if ch.isprintable()).split())
    return value if 2 <= len(value) <= 60 else None


class Controller(QObject):
    event_sig = Signal(object)          # CallEvent (from detector / local API threads)
    lookup_sig = Signal(str, object)    # call_id, CallerInfo
    session_sig = Signal(object, str)   # True / False / None, message
    ext_sig = Signal(object)            # payload dict pushed by the Chrome extension
    status_sig = Signal(str)            # tray tooltip text
    tray_msg_sig = Signal(str, str)     # tray balloon title, text
    company_sig = Signal(str, str)      # call_id, company name (stage 2: arrives after the name)

    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self.mock = cfg["minicrm"]["mock"]
        self.source = cfg["minicrm"]["cookie_source"]
        self.client = MiniCrmClient(cfg["minicrm"])
        self.pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="crm")
        self.overlay: CallOverlay | None = None
        self.current_id: str | None = None
        self.services = []
        self.ext_seen = False
        self.session_state = None
        self.api_service = None
        self.bria_service = None
        self.ext_last = None            # time.time() of the last extension push
        self.bria_last_event = ""       # HH:MM:SS of the last call event that came from Bria
        self.status = StatusModel()     # everything the status window shows
        self.health_timer = QTimer(self)
        self.health_timer.setInterval(10_000)
        self.health_timer.timeout.connect(self.refresh_health)
        self.event_sig.connect(self.on_event)
        self.lookup_sig.connect(self.on_lookup)
        self.session_sig.connect(self.on_session)
        self.ext_sig.connect(self.on_ext)
        self.company_sig.connect(self.on_company)

    # ---- lifecycle ---------------------------------------------------------------------
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
        name = _clean_user_name(payload.get("userName"))
        if name:
            self.status.set_user_name(name)
        elif not cookies:                     # logged out in Chrome: drop the greeting name
            self.status.set_user_name(None)
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
            text = "MiniCRM: mock mode"
        else:
            text = {True: "MiniCRM: logged in", False: "MiniCRM: login needed",
                    None: "MiniCRM: waiting for Chrome extension"}[ok if ok in (True, False) else None]
        self.status_sig.emit("Call Companion - " + text)
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
            self._show_notice("MiniCRM login needed",
                              "Please log in to MiniCRM in Chrome so caller names can be resolved.")
        # Tell the status window a session check has completed (drives the re-check button feedback).
        self.status.recheck_finished.emit()

    def _watchdog(self):
        if not self.ext_seen:
            self.tray_msg_sig.emit("Call Companion",
                                   "Chrome extension not connected. Start Chrome with the "
                                   "Call Companion extension to enable caller names.")

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
        return {"margin": ui["margin_px"], "answered_fade_ms": ui["answered_fade_ms"]}

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
        self.overlay = ov
        return ov

    def _on_overlay_closed(self, ov):
        if self.overlay is ov:   # a late signal from an overlay that was already replaced is ignored
            self.overlay = None

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
        log.info("event: %s %s %s (%s)", ev.kind, ev.number, ev.call_id, ev.source)
        if ev.source not in ("sim", "http"):
            self.bria_last_event = time.strftime("%H:%M:%S")
        if ev.kind == "answered":
            self.status.call_answered(ev.call_id)
        elif ev.kind == "ended":
            self.status.call_ended(ev.call_id)
        if ev.kind == "ringing":
            self.status.call_ringing(ev.call_id, ev.number)
            self._drop_overlay()
            self.current_id = ev.call_id
            self._new_overlay().show_ringing(ev.number)    # immediate: number only
            fut = self.pool.submit(self._safe_lookup, ev.number)
            fut.add_done_callback(lambda f, cid=ev.call_id: self.lookup_sig.emit(cid, f.result()))
        elif ev.call_id == self.current_id:
            ov = self._live_overlay()
            if ev.kind == "answered":
                if ov:
                    ov.set_answered()
            elif ev.kind == "ended":
                if ov:
                    ov.end()
                self.current_id = None   # reset even if the card already faded out (answered view)

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
        ov = self._live_overlay()
        if call_id == self.current_id and ov:
            ov.set_caller(info)
        # Stage 2: the name is already on the card. Fetch the company in the background (optional, slower);
        # it pops in on the third line when it arrives and is dropped if the call has moved on.
        if (call_id == self.current_id and info.found and not info.company
                and self.client.company_enabled()):
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
        ov = self._live_overlay()
        if call_id == self.current_id and ov:
            ov.set_company(company)


SIM_SCENARIOS = {                      # (delay in ms, event kind)
    "answered": [(0, "ringing"), (5000, "answered"), (9000, "ended")],
    "missed":   [(0, "ringing"), (6000, "ended")],
    "long":     [(0, "ringing"), (15000, "ended")],
}


def simulate(ctl: Controller, raw_number: str, scenario: str = "answered"):
    n = normalize_number(raw_number)
    cid = f"sim-{n}-{int(time.time() * 1000)}"       # unique per run, so repeats are separate log rows
    for delay, kind in SIM_SCENARIOS.get(scenario, SIM_SCENARIOS["answered"]):
        QTimer.singleShot(delay, lambda kind=kind: ctl.event_sig.emit(CallEvent(kind, cid, n, "sim")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--simulate", metavar="NUMBER", help="fake a call: ring -> answer -> end")
    ap.add_argument("--probe-bria", nargs="?", const=90, type=int, metavar="SECONDS",
                    help="capture Bria log lines during a test call into bria_probe.txt")
    ap.add_argument("--show", action="store_true", help="open the status window at startup")
    ap.add_argument("--version", action="store_true", help="print the version and exit")
    ap.add_argument("--install-autostart", action="store_true")
    ap.add_argument("--remove-autostart", action="store_true")
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
    ctl = Controller(cfg)
    win = StatusWindow(ctl.status, data_dir=str(config.data_dir()))
    win.recheck_requested.connect(ctl.check_session)
    win.simulate_requested.connect(lambda number, scenario: simulate(ctl, number, scenario))

    tray = QSystemTrayIcon(make_icon(), app)
    tray.setToolTip("Call Companion")
    menu = QMenu()
    open_act = menu.addAction("Status window")
    open_act.triggered.connect(win.show_and_raise)
    bold = open_act.font()
    bold.setBold(True)
    open_act.setFont(bold)
    menu.addSeparator()
    menu.addAction("Test incoming call").triggered.connect(lambda: simulate(ctl, "+36301234567"))
    menu.addAction("Re-check MiniCRM session").triggered.connect(ctl.check_session)
    menu.addSeparator()
    menu.addAction("Quit").triggered.connect(lambda: (win.prepare_quit(), ctl.stop(), app.quit()))
    tray.setContextMenu(menu)
    ctl.status_sig.connect(tray.setToolTip)
    ctl.tray_msg_sig.connect(lambda t, m: tray.showMessage(
        t, m, QSystemTrayIcon.MessageIcon.Warning, 8000))
    attach_to_tray(tray, win)             # double-click the tray icon -> status window
    tray.show()

    ctl.start_services()
    ctl.check_session()
    if args.show:
        QTimer.singleShot(300, win.show_and_raise)
    if args.simulate:
        QTimer.singleShot(600, lambda: simulate(ctl, args.simulate))
    code = app.exec()
    lock.unlock()
    sys.exit(code)


if __name__ == "__main__":
    main()
