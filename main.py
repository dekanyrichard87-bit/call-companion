"""Call Companion - entry point (tray app, controller, autostart helper)."""
import argparse
import logging
import sys
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


class Controller(QObject):
    event_sig = Signal(object)          # CallEvent (from detector / local API threads)
    lookup_sig = Signal(str, object)    # call_id, CallerInfo
    session_sig = Signal(object, str)   # True / False / None, message
    ext_sig = Signal(object)            # payload dict pushed by the Chrome extension
    status_sig = Signal(str)            # tray tooltip text
    tray_msg_sig = Signal(str, str)     # tray balloon title, text

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
        self.event_sig.connect(self.on_event)
        self.lookup_sig.connect(self.on_lookup)
        self.session_sig.connect(self.on_session)
        self.ext_sig.connect(self.on_ext)

    # ---- lifecycle ---------------------------------------------------------------------
    def start_services(self):
        la = self.cfg["local_api"]
        if la["enabled"]:
            self.services.append(LocalApi(la, self.event_sig.emit, self.ext_sig.emit))
        bria = self.cfg["detectors"]["bria_log"]
        if bria["enabled"]:
            self.services.append(BriaLogDetector(bria, self.event_sig.emit))
        for s in self.services:
            s.start()
        if not self.mock and self.source == "extension":
            QTimer.singleShot(30_000, self._watchdog)

    def stop(self):
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
        cookies = [c for c in payload.get("cookies", [])
                   if isinstance(c, dict) and isinstance(c.get("name"), str)
                   and isinstance(c.get("value"), str)]
        ua = payload.get("userAgent")
        self.ext_seen = True
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
        if ok is False and prev is not False:
            self._show_notice("MiniCRM login needed",
                              "Please log in to MiniCRM in Chrome so caller names can be resolved.")

    def _watchdog(self):
        if not self.ext_seen:
            self.tray_msg_sig.emit("Call Companion",
                                   "Chrome extension not connected. Start Chrome with the "
                                   "Call Companion extension to enable caller names.")

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
        if ev.kind == "ringing":
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
        ov = self._live_overlay()
        if call_id == self.current_id and ov:
            ov.set_caller(info)


def simulate(ctl: Controller, raw_number: str):
    n = normalize_number(raw_number)
    cid = "sim-" + n
    ctl.event_sig.emit(CallEvent("ringing", cid, n, "sim"))
    QTimer.singleShot(5000, lambda: ctl.event_sig.emit(CallEvent("answered", cid, n, "sim")))
    QTimer.singleShot(9000, lambda: ctl.event_sig.emit(CallEvent("ended", cid, n, "sim")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--simulate", metavar="NUMBER", help="fake a call: ring -> answer -> end")
    ap.add_argument("--probe-bria", nargs="?", const=90, type=int, metavar="SECONDS",
                    help="capture Bria log lines during a test call into bria_probe.txt")
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

    tray = QSystemTrayIcon(make_icon(), app)
    tray.setToolTip("Call Companion")
    menu = QMenu()
    menu.addAction("Test incoming call").triggered.connect(lambda: simulate(ctl, "+36301234567"))
    menu.addAction("Re-check MiniCRM session").triggered.connect(ctl.check_session)
    menu.addSeparator()
    menu.addAction("Quit").triggered.connect(lambda: (ctl.stop(), app.quit()))
    tray.setContextMenu(menu)
    ctl.status_sig.connect(tray.setToolTip)
    ctl.tray_msg_sig.connect(lambda t, m: tray.showMessage(
        t, m, QSystemTrayIcon.MessageIcon.Warning, 8000))
    tray.show()

    ctl.start_services()
    ctl.check_session()
    if args.simulate:
        QTimer.singleShot(600, lambda: simulate(ctl, args.simulate))
    code = app.exec()
    lock.unlock()
    sys.exit(code)


if __name__ == "__main__":
    main()
