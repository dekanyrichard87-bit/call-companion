"""Bria Desktop API client (v0.8.0): hold / resume / blind transfer of the running call.

Bria (5.x / 6.x, Stretto, Enterprise) listens on a local secure WebSocket:
    wss://cpclientapi.softphone.com:9002/counterpath/socketapi/v1
(cpclientapi.softphone.com resolves to 127.0.0.1; we connect to 127.0.0.1 directly and send that host name in the
TLS SNI / Host header, so no DNS lookup is needed). Messages are HTTP-like text frames with an XML body:

    GET /hold                               HTTP/1.1 200 OK            POST /statusChange
    User-Agent: CallCompanion/0.8.0         Transaction-ID: 7f...      ... <event type="call"/>
    Transaction-ID: 7f...                   Content-Type: application/xml
    Content-Type: application/xml           Content-Length: 0
    Content-Length: 87

    <?xml version="1.0" encoding="utf-8" ?>
    <holdCall><callId>...</callId></holdCall>

Requests used: GET /status (type call / systemInformation), GET /hold, GET /resume, GET /transferCall
(transfer needs Bria 6.2+). Bria's API call id is not the SIP Call-ID in the log, so every action first asks
GET /status "call" and picks the call whose participant number matches the card's number.

The user must allow the access once in Bria (Bria shows a prompt: "Allow access always"). The WebSocket client is a
minimal RFC 6455 implementation on the standard library (socket + ssl): no extra package, one short connection per
action (connect, status, action, close: typically 20-60 ms on the same PC). Runs on a worker thread; never call it on
the Qt main thread.

TLS: the connection never leaves the PC (127.0.0.1). Bria's embedded certificate can be expired or self-signed, so
certificate checking is off unless `bria_api.verify_tls` is true.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import socket
import ssl
import struct
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

log = logging.getLogger("callcompanion.bria_api")

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
XML_DECL = '<?xml version="1.0" encoding="utf-8" ?>'
DEFAULTS = {
    "enabled": True,
    "host": "cpclientapi.softphone.com",
    "connect_host": "127.0.0.1",
    "port": 9002,
    "path": "/counterpath/socketapi/v1",
    "timeout_s": 3,
    "verify_tls": False,
}


# The first request waits for Bria's permission prompt ("Allow access always"); it usually times out once.
TIMEOUT_TEXT = "A Bria nem válaszolt időben – ha a Bria engedélyt kér, engedélyezze, és próbálja újra"


class BriaApiError(Exception):
    """`code` is short and stable (for logs / the problem report); `user_text` is plain Hungarian for the card."""

    def __init__(self, code: str, user_text: str, detail: str = ""):
        super().__init__(f"{code}: {detail or user_text}")
        self.code, self.user_text, self.detail = code, user_text, detail


@dataclass
class BriaCall:
    call_id: str
    hold: str = ""                         # offHold | localHold | remoteHold | localAndRemoteHold ...
    numbers: list = field(default_factory=list)
    states: list = field(default_factory=list)

    @property
    def held_locally(self) -> bool:
        return self.hold.lower().startswith("local")


def digits_tail(s: str, n: int = 9) -> str:
    """Last n digits of the user part of a number / SIP URI ('sip:06301234567@pbx' -> '301234567')."""
    s = str(s or "")
    m = re.search(r"(?:sips?:|tel:)?([^@;>]+)", s)
    user = m.group(1) if m else s
    return re.sub(r"\D", "", user)[-n:]


def xml_escape(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


def parse_calls(xml_text: str) -> list[BriaCall]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise BriaApiError("BRIA-API-PARSE", "Érthetetlen válasz a Briától", str(e))
    calls = []
    for c in root.iter("call"):
        cid = (c.findtext("id") or "").strip()
        if not cid:
            continue
        bc = BriaCall(cid, (c.findtext("holdStatus") or "").strip())
        for p in c.iter("participant"):
            bc.numbers.append((p.findtext("number") or "").strip())
            bc.states.append((p.findtext("state") or "").strip())
        calls.append(bc)
    return calls


def pick_call(calls: list[BriaCall], number: str) -> BriaCall | None:
    """The call that belongs to the card's number. With exactly one call in Bria, that one (the PBX may present the
    number differently, e.g. with a prefix)."""
    want = digits_tail(number)
    if want:
        hits = [c for c in calls if any(digits_tail(n) == want for n in c.numbers if n)]
        if hits:
            connected = [c for c in hits if any(s.lower() == "connected" for s in c.states)]
            return (connected or hits)[0]
    return calls[0] if len(calls) == 1 else None


# ---- minimal WebSocket (RFC 6455) client ------------------------------------------------------------------------
class _WebSocket:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self._buf = b""

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise ConnectionError("connection closed by Bria")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def handshake(self, host: str, port: int, path: str):
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nOrigin: https://{host}\r\n\r\n")
        self.sock.sendall(req.encode())
        while b"\r\n\r\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("connection closed during the WebSocket handshake")
            self._buf += chunk
            if len(self._buf) > 16384:
                raise ConnectionError("handshake answer too long")
        head, self._buf = self._buf.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        if " 101 " not in lines[0] + " ":
            raise ConnectionError(f"handshake refused: {lines[0]}")
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        hdr = {ln.split(":", 1)[0].strip().lower(): ln.split(":", 1)[1].strip() for ln in lines[1:] if ":" in ln}
        if hdr.get("sec-websocket-accept") != accept:
            raise ConnectionError("bad Sec-WebSocket-Accept")

    def send(self, opcode: int, payload: bytes):
        head = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack("!Q", n)
        mask = os.urandom(4)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(head + mask + body)

    def send_text(self, text: str):
        self.send(0x1, text.encode("utf-8"))

    def recv_text(self) -> str:
        parts, opcode = [], None
        while True:
            b1, b2 = self._read_exact(2)
            fin, op = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read_exact(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if b2 & 0x80 else None
            data = self._read_exact(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 0x8:                                   # close
                raise ConnectionError("Bria closed the connection")
            if op == 0x9:                                   # ping -> pong
                self.send(0xA, data)
                continue
            if op == 0xA:
                continue
            if op in (0x1, 0x2):
                opcode, parts = op, [data]
            elif op == 0x0:
                parts.append(data)
            if fin and opcode is not None:
                return b"".join(parts).decode("utf-8", errors="replace")

    def close(self):
        try:
            self.send(0x8, struct.pack("!H", 1000))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def _parse_message(text: str):
    """-> (kind, status_code, headers, body). kind: 'response' | 'event' | 'other'."""
    first, _, rest = text.partition("\r\n")
    if "\r\n" not in text:
        first, _, rest = text.partition("\n")
    idx = rest.find("<")
    head, body = (rest[:idx], rest[idx:]) if idx >= 0 else (rest, "")
    headers = {}
    for ln in re.split(r"\r?\n", head):
        if ":" in ln:
            k, v = ln.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    m = re.match(r"HTTP/\d\.\d\s+(\d{3})", first.strip())
    if m:
        return "response", int(m.group(1)), headers, body
    if first.startswith("POST"):
        return "event", 0, headers, body
    return "other", 0, headers, body


class BriaApi:
    def __init__(self, cfg: dict | None = None, user_agent: str = "CallCompanion"):
        self.s = dict(DEFAULTS)
        self.s.update({k: v for k, v in (cfg or {}).items() if v is not None})
        self.user_agent = user_agent

    @property
    def enabled(self) -> bool:
        return bool(self.s.get("enabled", True))

    # ---- connection -----------------------------------------------------------------------------------------
    def _open(self) -> _WebSocket:
        s, timeout = self.s, float(self.s.get("timeout_s") or 3)
        try:
            raw = socket.create_connection((s["connect_host"], int(s["port"])), timeout=timeout)
        except ConnectionRefusedError as e:
            raise BriaApiError("BRIA-API-OFF", "A Bria nem fogad vezérlést – fut a Bria?", str(e))
        except socket.timeout as e:
            raise BriaApiError("BRIA-API-TIMEOUT", "A Bria nem válaszolt időben", str(e))
        except OSError as e:
            raise BriaApiError("BRIA-API-OFF", "A Bria nem érhető el", str(e))
        raw.settimeout(timeout)
        try:
            if s.get("tls", True):
                ctx = ssl.create_default_context()
                if not s.get("verify_tls"):
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                sock = ctx.wrap_socket(raw, server_hostname=s["host"])
            else:
                sock = raw                                     # tests only (plain ws://)
            ws = _WebSocket(sock)
            ws.handshake(s["host"], int(s["port"]), s["path"])
            return ws
        except (ssl.SSLError, ConnectionError, OSError) as e:
            raw.close()
            raise BriaApiError("BRIA-API-HANDSHAKE", "Nem sikerült kapcsolódni a Bria vezérléshez",
                               f"{e.__class__.__name__}: {e}")

    def _request(self, ws: _WebSocket, kind: str, body: str) -> tuple[int, str]:
        tid = uuid.uuid4().hex
        payload = (XML_DECL + "\r\n" + body) if body else ""
        msg = (f"GET /{kind}\r\nUser-Agent: {self.user_agent}\r\nTransaction-ID: {tid}\r\n"
               f"Content-Type: application/xml\r\nContent-Length: {len(payload.encode('utf-8'))}\r\n\r\n{payload}")
        ws.send_text(msg)
        deadline = time.monotonic() + float(self.s.get("timeout_s") or 3)
        while True:
            if time.monotonic() > deadline:
                raise BriaApiError("BRIA-API-TIMEOUT", TIMEOUT_TEXT, f"GET /{kind}")
            try:
                text = ws.recv_text()
            except socket.timeout as e:
                raise BriaApiError("BRIA-API-TIMEOUT", TIMEOUT_TEXT, f"GET /{kind}: {e}")
            except (ConnectionError, OSError) as e:
                raise BriaApiError("BRIA-API-CLOSED", "A Bria bontotta a vezérlő kapcsolatot – engedélyezze a "
                                   "hozzáférést a Briában", f"GET /{kind}: {e}")
            kind_, code, headers, rbody = _parse_message(text)
            if kind_ != "response":
                continue                                        # status-change events: not ours
            rtid = headers.get("transaction-id")
            if rtid and rtid != tid:
                continue
            return code, rbody

    def _session(self, fn):
        t0 = time.monotonic()
        ws = self._open()
        try:
            return fn(ws)
        finally:
            ws.close()
            log.info("bria api: session %d ms", int((time.monotonic() - t0) * 1000))

    def _calls(self, ws) -> list[BriaCall]:
        code, body = self._request(ws, "status", "<status>\r\n <type>call</type>\r\n</status>")
        if code != 200:
            raise BriaApiError("BRIA-API-HTTP", f"A Bria elutasította a kérést ({code})", f"status call -> {code}")
        return parse_calls(body) if body.strip() else []

    def _call_for(self, ws, number: str) -> BriaCall:
        calls = self._calls(ws)
        call = pick_call(calls, number)
        log.info("bria api: %d call(s) in Bria, match for ...%s -> %s", len(calls), digits_tail(number, 4),
                 call.call_id if call else None)
        if call is None:
            raise BriaApiError("BRIA-API-NOCALL", "A hívás nem található a Briában",
                               f"{len(calls)} call(s), none matches")
        return call

    # ---- public (worker thread) -----------------------------------------------------------------------------
    def check(self) -> str:
        """Quick health check for the developer window: number of calls and, when Bria tells it, its version."""
        def run(ws):
            calls = self._calls(ws)
            version = ""
            try:
                code, body = self._request(ws, "status", "<status>\r\n <type>systemInformation</type>\r\n</status>")
                if code == 200 and body:
                    root = ET.fromstring(body)
                    version = (root.findtext(".//version") or "").strip()
            except (BriaApiError, ET.ParseError):
                pass
            return f"Bria válaszol{(' (' + version + ')') if version else ''} • {len(calls)} folyamatban lévő hívás"
        return self._session(run)

    def set_hold(self, number: str, hold: bool) -> bool:
        """Put the call with this number on hold (hold=True) or take it back. Returns the new hold state.
        Nothing is sent when Bria already has the wanted state (the user may have used Bria's own button)."""
        def run(ws):
            call = self._call_for(ws, number)
            if call.held_locally == hold:
                log.info("bria api: call %s already %s", call.call_id, "on hold" if hold else "off hold")
                return hold
            tag = "holdCall" if hold else "resumeCall"
            code, _ = self._request(ws, "hold" if hold else "resume",
                                    f"<{tag}>\r\n <callId>{xml_escape(call.call_id)}</callId>\r\n</{tag}>")
            if code != 200:
                raise BriaApiError("BRIA-API-HTTP", f"A Bria nem tudta {'tartásba tenni' if hold else 'visszavenni'}"
                                   f" a hívást ({code})", f"{tag} -> {code}")
            log.info("bria api: %s %s -> 200", tag, call.call_id)
            return hold
        return self._session(run)

    def transfer(self, number: str, target: str) -> None:
        """Blind transfer of the call with this number to `target` (an extension). Bria 6.2+."""
        target = re.sub(r"[^0-9*#+]", "", str(target or ""))
        if not target:
            raise BriaApiError("BRIA-API-TARGET", "Érvénytelen mellékszám")

        def run(ws):
            call = self._call_for(ws, number)
            code, _ = self._request(ws, "transferCall",
                                    f"<transferCall>\r\n <callId>{xml_escape(call.call_id)}</callId>\r\n"
                                    f" <target>{xml_escape(target)}</target>\r\n</transferCall>")
            if code != 200:
                raise BriaApiError("BRIA-API-HTTP", f"A Bria nem tudta átkapcsolni a hívást ({code}) – a Bria "
                                   "6.2 vagy újabb verziója szükséges", f"transferCall -> {code}")
            log.info("bria api: transferCall %s -> %s: 200", call.call_id, target)
        self._session(run)
