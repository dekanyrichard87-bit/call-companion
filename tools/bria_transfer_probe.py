"""Bria transfer probe v3 (diagnosis for 0.9.0-rc): why does GET /transferCall answer 400 on Bria Enterprise 6.8?

Diagnostic tool: not bundled in the exe / installer.
Run DURING an established test call (Bria connected to someone, e.g. your own mobile):
    cd d:\\callcompanion\\call-companion
    .venv\\Scripts\\python tools\\bria_transfer_probe.py

1. It lists the calls Bria reports (raw XML, digits masked except the last 3).
2. It asks for the extension to transfer to (e.g. 105), and optionally the SIP domain and user-name prefix of the
   account (Bria > Account settings; Enter = skip the variants that need them).
3. It tries the known message variants one after the other and stops at the first one Bria accepts (200):
     v3: three control requests (bogus hold, unknown command, transfer with a bogus call id), then
     GET /transferCall with the target as: ext | ext@domain | sip:ext@domain | prefix+ext | prefix+ext@domain |
     sip:prefix+ext@domain
   On success the call really IS transferred (that is the test).
Everything is printed and saved to %LOCALAPPDATA%\\CallCompanion\\bria_transfer_probe.txt (send that file).
"""
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # repo root: bria_api.py
try:
    from bria_api import BriaApi, BriaApiError, parse_calls, xml_escape
except ImportError:
    sys.exit("A tools mappát a repóban hagyd (d:\\callcompanion\\call-companion\\tools), onnan futtasd.")

LINES = []


def out(s=""):
    line = f"{datetime.now():%H:%M:%S}  {s}"
    print(line, flush=True)
    LINES.append(line)


def mask(s):
    return re.sub(r"\d(?=\d{3})", "#", s)


def send(api, ws, kind, body):
    """Raw request through bria_api's connection; returns (code, body)."""
    code, rbody = api._request(ws, kind, body)
    return code, rbody


def main():
    api = BriaApi({"timeout_s": 5}, "CallCompanion/probe")
    out("Bria transfer probe v3")
    try:
        ws = api._open()
    except BriaApiError as e:
        out(f"Nem sikerült kapcsolódni: {e.code} {e.detail}")
        return
    try:
        code, body = send(api, ws, "status", "<status>\r\n <type>call</type>\r\n</status>")
        out(f"status call -> {code}")
        out("RAW: " + mask(body.replace("\r", "").replace("\n", " "))[:1500])
        calls = parse_calls(body) if body.strip() else []
        if not calls:
            out("Nincs folyamatban lévő hívás a Briában. Indíts egy hívást, vedd fel, és futtasd újra.")
            return
        for c in calls:
            out(f"call id={c.call_id} hold={c.hold!r} numbers={[mask(n) for n in c.numbers]} states={c.states}")
        call = calls[0]
        code, body = send(api, ws, "status", "<status>\r\n <type>systemInformation</type>\r\n</status>")
        out("systemInformation: " + re.sub(r"\s+", " ", body)[:400])
        code, body = send(api, ws, "status", "<status>\r\n <type>systemSettings</type>\r\n</status>")
        out("systemSettings: " + mask(re.sub(r"\s+", " ", body))[:1200])
        target = input("\nMelyik mellékre kapcsoljuk át? (pl. 105, Enter = kilépés): ").strip()
        if not target:
            return
        dom_in = input("SIP domain (pl. sip.szolgaltato.hu) [Enter = kihagyás]: ").strip()
        pre_in = input("SIP felhasználónév előtag (pl. cegnev_) [Enter = kihagyás]: ").strip()
        out(f"target={target}  domain={dom_in or '-'}  prefix={pre_in or '-'}  call={call.call_id}  hold={call.hold!r}")

        # control tests: how does Bria answer a request it surely understands but cannot do, and one it cannot know?
        controls = [
            ("control 1: GET /hold with a bogus callId", "hold",
             "<holdCall>\r\n <callId>00000000000000000000000000000000</callId>\r\n</holdCall>"),
            ("control 2: GET /noSuchCommand", "noSuchCommand", "<noSuchCommand/>"),
            ("control 3: GET /transferCall with a bogus callId", "transferCall",
             "<transferCall>\r\n <callId>00000000000000000000000000000000</callId>\r\n <target>"
             + xml_escape(target) + "</target>\r\n</transferCall>"),
        ]
        for label, kind, body in controls:
            try:
                code, _ = send(api, ws, kind, body)
                out(f"{label} -> {code}")
            except BriaApiError as e:
                out(f"{label} -> {e.code} ({e.detail})")
                ws.close()
                ws = api._open()                 # Bria may drop the connection after an unknown request
        cid = xml_escape(call.call_id)
        targets = [target]
        if dom_in:
            targets += [f"{target}@{dom_in}", f"sip:{target}@{dom_in}"]
        if pre_in:
            targets.append(f"{pre_in}{target}")
            if dom_in:
                targets += [f"{pre_in}{target}@{dom_in}", f"sip:{pre_in}{target}@{dom_in}"]
        variants = []
        for i, tgt in enumerate(targets):
            variants.append((chr(ord("A") + i), "transferCall",
                             f"<transferCall>\r\n <callId>{cid}</callId>\r\n <target>{xml_escape(tgt)}</target>"
                             f"\r\n</transferCall>"))
        out("targets: " + " | ".join(f"{chr(ord('A') + i)}={t}" for i, t in enumerate(targets)))
        for name, kind, body in variants:
            code, rbody = send(api, ws, kind, body)
            flat = re.sub(r"\s+", " ", rbody)[:300]
            out(f"variant {name}: GET /{kind} -> {code}  body={flat!r}")
            if code == 200:
                out(f"SIKER: a(z) {name} változatot fogadta el a Bria.")
                break
        else:
            out("Egyik változatot sem fogadta el a Bria.")
    except BriaApiError as e:
        out(f"Hiba: {e.code} {e.user_text} ({e.detail})")
    finally:
        ws.close()
        path = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "CallCompanion",
                            "bria_transfer_probe.txt")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(LINES) + "\n")
        print(f"\nMentve: {path}")


if __name__ == "__main__":
    main()
