"""Local HTTP API on 127.0.0.1 used by (a) the Chrome extension and (b) manual/test call events.

Routes
  POST /ext/session   extension -> app: MiniCRM cookies + user agent   (Origin must be chrome-extension://)
  GET  /ext/ping      extension reachability check                     (Origin must be chrome-extension://)
  GET  /event?type=ring|answer|end&number=..&id=..[&dir=in|out][&reason=cancel|bye|rejected|failed|elsewhere]
                      manual / test call events (dir defaults to in; reason is only used with type=end)

Hardening: Host header must be 127.0.0.1/localhost (DNS-rebinding), extension routes require a
chrome-extension:// Origin (optionally pinned to one extension id), /event refuses browser-initiated
requests (Sec-Fetch-Site present), body size is capped, cookie values are never logged.
"""
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from events import CallEvent, normalize_number

log = logging.getLogger(__name__)
MAX_BODY = 256 * 1024
KINDS = {"ring": "ringing", "answer": "answered", "end": "ended"}
DIRECTIONS = ("in", "out")
REASONS = ("", "cancel", "bye", "rejected", "failed", "elsewhere")


class LocalApi(threading.Thread):
    def __init__(self, cfg: dict, emit_event, on_session):
        super().__init__(daemon=True, name="local-api")
        self.cfg, self.emit_event, self.on_session = cfg, emit_event, on_session
        self.httpd = None

    def run(self):
        cfg, emit_event, on_session = self.cfg, self.emit_event, self.on_session
        port = cfg["port"]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

        class Handler(BaseHTTPRequestHandler):
            def _send(self, code, payload=None):
                data = json.dumps(payload).encode() if payload is not None else b""
                self.send_response(code)
                if data:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _host_ok(self):
                return self.headers.get("Host", "").lower() in hosts

            def _from_extension(self):
                origin = self.headers.get("Origin", "")
                if not origin.startswith("chrome-extension://"):
                    return False
                pin = cfg.get("extension_id", "").strip()
                return not pin or origin == "chrome-extension://" + pin

            def do_OPTIONS(self):
                self._send(403, {"error": "forbidden"})

            def do_GET(self):
                if not self._host_ok():
                    return self._send(403, {"error": "bad host"})
                u = urlparse(self.path)
                if u.path == "/ext/ping":
                    if not self._from_extension():
                        return self._send(403, {"error": "bad origin"})
                    return self._send(200, {"ok": True})
                if u.path == "/event":
                    if self.headers.get("Sec-Fetch-Site"):   # browsers always send it; curl/PowerShell don't
                        return self._send(403, {"error": "browser requests not allowed"})
                    q = {k: v[0] for k, v in parse_qs(u.query).items()}
                    kind = KINDS.get(q.get("type", ""))
                    if not kind:
                        return self._send(400, {"error": "type must be ring|answer|end"})
                    direction, reason = q.get("dir", "in"), q.get("reason", "")
                    if direction not in DIRECTIONS or reason not in REASONS:
                        return self._send(400, {"error": "dir must be in|out, reason one of " + "|".join(REASONS[1:])})
                    number = normalize_number(q.get("number", ""))
                    emit_event(CallEvent(kind, q.get("id") or number or "http", number, "http", direction,
                                         reason if kind == "ended" else ""))
                    return self._send(204)
                self._send(404, {"error": "not found"})

            def do_POST(self):
                if not self._host_ok():
                    return self._send(403, {"error": "bad host"})
                if urlparse(self.path).path != "/ext/session":
                    return self._send(404, {"error": "not found"})
                if not self._from_extension():
                    return self._send(403, {"error": "bad origin"})
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                    if n <= 0 or n > MAX_BODY:
                        return self._send(413, {"error": "bad size"})
                    payload = json.loads(self.rfile.read(n))
                    if not isinstance(payload, dict):
                        raise ValueError("not an object")
                except (ValueError, json.JSONDecodeError):
                    return self._send(400, {"error": "bad json"})
                on_session(payload)
                self._send(200, {"ok": True})

            def log_message(self, *a):
                pass

        try:
            self.httpd = ThreadingHTTPServer((cfg["host"], port), Handler)
        except OSError as e:
            log.error("Local API could not bind %s:%s: %s", cfg["host"], port, e)
            return
        log.info("Local API listening on %s:%s", cfg["host"], port)
        self.httpd.serve_forever()

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
