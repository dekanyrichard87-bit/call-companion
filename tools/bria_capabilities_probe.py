"""Bria capabilities probe: which Desktop API commands does THIS Bria accept?

Safe: every call-control command is sent for a NON-EXISTING call id, so nothing happens to real calls, and only
read-only status queries are sent otherwise. No call is needed. Nothing is dialled, muted or changed.
Answer 200 = Bria knows the command; 400 = Bria treats it as unknown (disabled / not available).
Diagnostic tool: not bundled in the exe / installer.

Run from the repo folder:
    cd d:\\callcompanion\\call-companion
    .venv\\Scripts\\python tools\\bria_capabilities_probe.py
Result also in %LOCALAPPDATA%\\CallCompanion\\bria_capabilities.txt
"""
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # repo root: bria_api.py
try:
    from bria_api import BriaApi, BriaApiError
except ImportError:
    sys.exit("A tools mappát a repóban hagyd (d:\\callcompanion\\call-companion\\tools), onnan futtasd.")

BOGUS = "00000000000000000000000000000000"
CONTROL = [  # (label, request, body) - all for a call that does not exist
    ("Hívás fogadása (answer)", "answer",
     f"<answerCall>\r\n <callId>{BOGUS}</callId>\r\n <withVideo>false</withVideo>\r\n</answerCall>"),
    ("Hívás bontása / elutasítása (endCall)", "endCall", f"<endCall>\r\n <callId>{BOGUS}</callId>\r\n</endCall>"),
    ("Tartás (hold)", "hold", f"<holdCall>\r\n <callId>{BOGUS}</callId>\r\n</holdCall>"),
    ("Folytatás (resume)", "resume", f"<resumeCall>\r\n <callId>{BOGUS}</callId>\r\n</resumeCall>"),
    ("Átkapcsolás vakon (transferCall)", "transferCall",
     f"<transferCall>\r\n <callId>{BOGUS}</callId>\r\n <target>999</target>\r\n</transferCall>"),
    ("Kérdezéses átkapcsolás (startAttendedTransferCall)", "startAttendedTransferCall",
     f"<startAttendedTransferCall>\r\n <callId>{BOGUS}</callId>\r\n <target>999</target>\r\n"
     f"</startAttendedTransferCall>"),
    ("Hívásrögzítés indítása (startRecording)", "startRecording",
     f"<startRecording>\r\n <callId>{BOGUS}</callId>\r\n <filename>x</filename>\r\n</startRecording>"),
    ("Ismeretlen parancs (ellenőrzés, 400 várható)", "noSuchCommand", "<noSuchCommand/>"),
]
STATUS = ["call", "phone", "systemInformation", "systemSettings", "audioProperties", "voiceMail", "missedCall",
          "callHistory", "authentication"]


def main():
    lines = []

    def out(s):
        line = f"{datetime.now():%H:%M:%S}  {s}"
        print(line, flush=True)
        lines.append(line)

    api = BriaApi({"timeout_s": 5}, "CallCompanion/probe")
    out("Bria capabilities probe")
    try:
        ws = api._open()
    except BriaApiError as e:
        out(f"Nem sikerült kapcsolódni: {e.code} {e.detail}")
        return
    try:
        out("--- vezérlő parancsok (nem létező hívásra) ---")
        for label, kind, body in CONTROL:
            try:
                code, _ = api._request(ws, kind, body)
                verdict = "ISMERI" if code == 200 else ("NEM ISMERI" if code == 400 else "?")
                out(f"{code}  {verdict:<10} {label}  [GET /{kind}]")
            except BriaApiError as e:
                out(f"ERR  {e.code:<10} {label}  [GET /{kind}] {e.detail}")
                ws.close()
                ws = api._open()
        out("--- állapot-lekérdezések (csak olvasás) ---")
        for t in STATUS:
            try:
                code, body = api._request(ws, "status", f"<status>\r\n <type>{t}</type>\r\n</status>")
                flat = re.sub(r"\d(?=\d{3})", "#", re.sub(r"\s+", " ", body))[:160]
                out(f"{code}  status {t:<18} {flat}")
            except BriaApiError as e:
                out(f"ERR  status {t:<18} {e.code} {e.detail}")
                ws.close()
                ws = api._open()
    finally:
        ws.close()
        path = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "CallCompanion",
                            "bria_capabilities.txt")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        print(f"\nMentve: {path}")


if __name__ == "__main__":
    main()
