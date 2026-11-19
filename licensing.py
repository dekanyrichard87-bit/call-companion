"""Licence handling - PREPARATION ONLY (v1.1.0). Nothing is restricted: every state lets the app run as before.

What exists now:
  * a licence file format, signed with Ed25519, checked offline (no server, no network);
  * a machine id (hash of Windows' MachineGuid) a licence can be bound to;
  * check() -> LicenseInfo with a plain Hungarian state for the Névjegy page and one log line at start;
  * tools/make_license.py: creates the key pair (once) and issues licence files. The PRIVATE key never goes into
    the repository or the app; only PUBLIC_KEY below is shipped.

Licence file: %LOCALAPPDATA%\\CallCompanion\\license.key, one line:
    CCLIC1.<base64url(JSON payload)>.<base64url(64-byte signature of the payload bytes)>
Payload (all optional except licensee):
    {"licensee": "Minta Kft.", "issued": "2026-11-19", "expires": "2027-11-19" | null,
     "seats": 3, "machines": ["<machine id>", ...], "features": ["transfer", ...]}

While PUBLIC_KEY is empty the app is "Belső használat" (internal use) and no file is read. Business rules (what
happens without a licence, trial period, seats) are NOT decided yet - decide them before any enforcement is added.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import platform
import sys
from dataclasses import dataclass, field
from datetime import date

log = logging.getLogger("callcompanion.license")

# Ed25519 public key (32 bytes, hex) made by `python tools/make_license.py new-key`. Empty = licensing not active.
PUBLIC_KEY = ""
PREFIX = "CCLIC1"
FILE_NAME = "license.key"


# ---- Ed25519 (RFC 8032 reference arithmetic, pure Python; a verify takes a few ms) --------------------------------
_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P)
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P:
        x = x * _I % _P
    return _P - x if x % 2 else x


_BY = 4 * pow(5, _P - 2, _P) % _P
_B = (_xrecover(_BY), _BY, 1, _xrecover(_BY) * _BY % _P)     # extended coordinates (X, Y, Z, T)


def _add(p, q):
    a = (p[1] - p[0]) * (q[1] - q[0]) % _P
    b = (p[1] + p[0]) * (q[1] + q[0]) % _P
    c = 2 * p[3] * q[3] * _D % _P
    d = 2 * p[2] * q[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return e * f % _P, g * h % _P, f * g % _P, e * h % _P


def _mul(s: int, p):
    q = (0, 1, 1, 0)
    while s:
        if s & 1:
            q = _add(q, p)
        p = _add(p, p)
        s >>= 1
    return q


def _encode(p) -> bytes:
    zi = pow(p[2], _P - 2, _P)
    x, y = p[0] * zi % _P, p[1] * zi % _P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decode(b: bytes):
    if len(b) != 32:
        raise ValueError("bad point")
    y = int.from_bytes(b, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= _P:
        raise ValueError("bad point")
    x = _xrecover(y)
    if (x * x * -1 + y * y - 1 - _D * x * x * y * y) % _P:
        raise ValueError("point not on curve")
    if (x & 1) != sign:
        x = _P - x
    return x, y, 1, x * y % _P


def _h(*parts: bytes) -> int:
    return int.from_bytes(hashlib.sha512(b"".join(parts)).digest(), "little")


def _secret(seed: bytes) -> tuple[int, bytes]:
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_from_seed(seed: bytes) -> bytes:
    return _encode(_mul(_secret(seed)[0], _B))


def sign(seed: bytes, msg: bytes) -> bytes:
    """Only tools/make_license.py signs (it holds the private seed)."""
    a, prefix = _secret(seed)
    pub = _encode(_mul(a, _B))
    r = _h(prefix, msg) % _L
    rb = _encode(_mul(r, _B))
    s = (r + _h(rb, pub, msg) * a) % _L
    return rb + int.to_bytes(s, 32, "little")


def verify(pub: bytes, msg: bytes, sig: bytes) -> bool:
    try:
        if len(sig) != 64:
            return False
        a = _decode(pub)
        r = _decode(sig[:32])
        s = int.from_bytes(sig[32:], "little")
        if s >= _L:
            return False
        k = _h(sig[:32], pub, msg) % _L
        lhs, rhs = _mul(s, _B), _add(r, _mul(k, a))
        return _encode(lhs) == _encode(rhs)
    except (ValueError, TypeError):
        return False


# ---- licence file -------------------------------------------------------------------------------------------------
def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def machine_id() -> str:
    """Stable id of this Windows installation: SHA-256 of HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid,
    first 16 hex digits (the GUID itself is not shown or sent anywhere)."""
    raw = ""
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                                winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
                raw = str(winreg.QueryValueEx(k, "MachineGuid")[0])
        except OSError:
            raw = ""
    raw = raw or ("node:" + platform.node())
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class LicenseInfo:
    state: str                        # internal | missing | valid | expired | invalid | other_machine
    text: str                         # plain Hungarian, for the Névjegy page
    licensee: str = ""
    expires: str | None = None
    features: list = field(default_factory=list)

    @property
    def ok(self) -> bool:             # not used to restrict anything yet
        return self.state in ("internal", "valid")


def parse(line: str, public_key_hex: str = PUBLIC_KEY, mid: str | None = None,
          today: date | None = None) -> LicenseInfo:
    try:
        prefix, body, sig = line.strip().split(".")
        if prefix != PREFIX:
            raise ValueError
        payload = _b64d(body)
        if not verify(bytes.fromhex(public_key_hex), payload, _b64d(sig)):
            return LicenseInfo("invalid", "A licenc érvénytelen (az aláírás nem stimmel)")
        data = json.loads(payload.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError):
        return LicenseInfo("invalid", "A licencfájl hibás")
    who = str(data.get("licensee") or "?")
    exp = data.get("expires") or None
    feats = list(data.get("features") or [])
    machines = [str(m) for m in (data.get("machines") or [])]
    if machines and (mid or machine_id()) not in machines:
        return LicenseInfo("other_machine", f"A licenc ({who}) egy másik gépre szól", who, exp, feats)
    if exp:
        try:
            if date.fromisoformat(exp) < (today or date.today()):
                return LicenseInfo("expired", f"A licenc lejárt: {exp} ({who})", who, exp, feats)
        except ValueError:
            return LicenseInfo("invalid", "A licencfájl hibás (lejárati dátum)")
    return LicenseInfo("valid", f"Licenc: {who}" + (f" • érvényes: {exp}-ig" if exp else ""), who, exp, feats)


def check(data_dir) -> LicenseInfo:
    """State at start. Never raises; never blocks anything (preparation only)."""
    if not PUBLIC_KEY:
        info = LicenseInfo("internal", "Belső használat")
    else:
        from pathlib import Path
        path = Path(data_dir) / FILE_NAME
        try:
            line = path.read_text(encoding="utf-8-sig")
        except OSError:
            info = LicenseInfo("missing", "Nincs licencfájl")
        else:
            info = parse(line)
    log.info("license: %s (%s)", info.state, info.text)
    return info
