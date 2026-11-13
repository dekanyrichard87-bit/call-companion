"""Bria capabilities probe v2: which Desktop API commands does THIS Bria accept?
(Preparation for "Bria as the hidden engine": dialling, DTMF, mute, merge, bring to front ...)

Part 1 - automatic and SAFE: every call-control command is sent for a NON-EXISTING call id, so nothing happens to
real calls; status queries only read. Answer 200 = Bria knows the command, 400 = unknown / not available.
Command names marked "guess" are not documented by name (CounterPath only lists the feature: DTMF, merge, mute ...),
so several spellings are tried; a 400 on a guess only means "not under this name".
Never sent: login / logout (could sign Bria out of the provider account), screenshare.

Part 2 - OPTIONAL, asked one by one (Enter = skip): things that cannot be tested without an effect:
  a) bring Bria's window to the front,  b) place a test call to a number you type (e.g. your own mobile),
  c) during a running call: DTMF "1" and mute on / off.

Run from the repo folder:
    cd d:\\callcompanion\\call-companion
    .venv\\Scripts\\python tools\\bria_capabilities_probe.py
Result also in %LOCALAPPDATA%\\CallCompanion\\bria_capabilities.txt (phone numbers masked). Diagnostic tool: not
bundled in the exe / installer.
"""
import os
import re
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # repo root: bria_api.py
try:
    from bria_api import BriaApi, BriaApiError, parse_calls, xml_escape
except ImportError:
    sys.exit("A tools mappát a repóban hagyd (d:\\callcompanion\\call-companion\\tools), onnan futtasd.")

B = "00000000000000000000000000000000"
B2 = "11111111111111111111111111111111"


def x(tag, inner=""):
    return f"<{tag}>\r\n{inner}</{tag}>"


# (label, request, body, documented?) - all for call ids that do not exist
CONTROL = [
    ("Hívás fogadása", "answer", x("answerCall", f" <callId>{B}</callId>\r\n <withVideo>false</withVideo>\r\n"), True),
    ("Bontás / elutasítás", "endCall", x("endCall", f" <callId>{B}</callId>\r\n"), True),
    ("Tartás", "hold", x("holdCall", f" <callId>{B}</callId>\r\n"), True),
    ("Folytatás", "resume", x("resumeCall", f" <callId>{B}</callId>\r\n"), True),
    ("Átkapcsolás vakon", "transferCall", x("transferCall", f" <callId>{B}</callId>\r\n <target>999</target>\r\n"),
     True),
    ("Kérdezéses átkapcsolás indítása", "startAttendedTransferCall",
     x("startAttendedTransferCall", f" <callId>{B}</callId>\r\n <target>999</target>\r\n"), True),
    ("Kérdezéses átkapcsolás befejezése", "completeAttendedTransferCall",
     x("completeAttendedTransferCall", f" <callId>{B}</callId>\r\n"), False),
    ("Hívások összevonása (konferencia)", "mergeCall",
     x("mergeCall", f" <callId>{B}</callId>\r\n <callId>{B2}</callId>\r\n"), False),
    ("Hívások összevonása (konferencia)", "mergeCalls",
     x("mergeCalls", f" <callId>{B}</callId>\r\n <callId>{B2}</callId>\r\n"), False),
    ("Hívások összevonása (konferencia)", "conference",
     x("conference", f" <callId>{B}</callId>\r\n <callId>{B2}</callId>\r\n"), False),
    ("DTMF (hangmenü billentyű)", "dtmf", x("dtmf", f" <callId>{B}</callId>\r\n <tone>1</tone>\r\n"), False),
    ("DTMF (hangmenü billentyű)", "sendDtmf", x("sendDtmf", f" <callId>{B}</callId>\r\n <tone>1</tone>\r\n"), False),
    ("DTMF (hangmenü billentyű)", "playDtmf", x("playDtmf", f" <callId>{B}</callId>\r\n <tone>1</tone>\r\n"), False),
    ("Némítás hívásra", "muteCall", x("muteCall", f" <callId>{B}</callId>\r\n <mute>false</mute>\r\n"), False),
    ("Hívásrögzítés indítása", "startRecording",
     x("startRecording", f" <callId>{B}</callId>\r\n <filename>x</filename>\r\n"), True),
    ("Hívásrögzítés leállítása", "stopRecording", x("stopRecording", f" <callId>{B}</callId>\r\n"), False),
    ("Ellenőrzés: ismeretlen parancs (400 várható)", "noSuchCommand", "<noSuchCommand/>", True),
]
STATUS = ["phone", "call", "callHistory", "missedCall", "voiceMail", "audioProperties", "systemSettings",
          "systemInformation", "authentication",
          # guesses (read-only, harmless):
          "account", "accounts", "presence", "contacts", "callForwarding", "doNotDisturb"]


def mask(s: str) -> str:
    return re.sub(r"\d(?=\d{3})", "#", re.sub(r"\s+", " ", s or ""))


def main():
    lines = []

    def out(s=""):
        line = f"{datetime.now():%H:%M:%S}  {s}"
        print(line, flush=True)
        lines.append(line)

    api = BriaApi({"timeout_s": 5}, "CallCompanion/probe")
    out("Bria capabilities probe v2")
    try:
        ws = api._open()
    except BriaApiError as e:
        out(f"Nem sikerült kapcsolódni: {e.code} {e.detail}")
        return

    def req(kind, body):
        nonlocal ws
        try:
            return api._request(ws, kind, body)
        except BriaApiError as e:
            try:
                ws.close()
            except Exception:
                pass
            ws = api._open()                    # Bria may drop the connection after an unknown request
            return f"ERR {e.code}", e.detail

    try:
        out("=== 1. rész: vezérlő parancsok, NEM LÉTEZŐ hívásra (biztonságos) ===")
        for label, kind, body, documented in CONTROL:
            code, _ = req(kind, body)
            verdict = "ISMERI" if code == 200 else ("NEM ISMERI" if code == 400 else "?")
            out(f"{code}  {verdict:<10} {label}  [GET /{kind}]{'' if documented else '  (név: találgatás)'}")
        out("=== állapot-lekérdezések (csak olvasás) ===")
        for t in STATUS:
            code, body = req("status", x("status", f" <type>{t}</type>\r\n"))
            out(f"{code}  status {t:<16} {mask(body)[:220]}")

        out("=== 2. rész: opcionális próbák (mindegyikre rákérdez; Enter = kihagyás) ===")
        if input("\na) Előhozzam a Bria ablakát (bringToFront)? [i/N]: ").strip().lower() in ("i", "igen", "y"):
            code, body = req("bringToFront", "<bringToFront/>")
            out(f"{code}  bringToFront  {mask(body)[:120]}")
        num = input("\nb) Próbahívás (GET /call). Írj be egy számot, amit hívhatok (pl. a saját mobilod),\n"
                    "   vagy Enter = kihagyás: ").strip()
        if num:
            body = x("dial", f" <type>audio</type>\r\n <number>{xml_escape(num)}</number>\r\n"
                             f" <displayName>Call Companion teszt</displayName>\r\n")
            code, rbody = req("call", body)
            out(f"{code}  call (tárcsázás) -> {mask(num)}  {mask(rbody)[:120]}")
            if code != 200:
                body2 = f'<dial type="audio">\r\n <number>{xml_escape(num)}</number>\r\n</dial>'
                code, rbody = req("call", body2)
                out(f"{code}  call (2. változat, type attribútum)  {mask(rbody)[:120]}")
            if code == 200:
                out("A hívásnak most el kell indulnia a Briában. Vedd fel a mobilodon (a c) próbához kell).")
                time.sleep(2)
        if input("\nc) Fut most hívás? Ha igen, kipróbálom rajta a DTMF '1'-et és a némítást (be, majd ki).\n"
                 "   [i/N]: ").strip().lower() in ("i", "igen", "y"):
            code, body = req("status", x("status", " <type>call</type>\r\n"))
            calls = parse_calls(body) if code == 200 and body.strip() else []
            if not calls:
                out("Nincs folyamatban lévő hívás a Briában - c) kihagyva.")
            else:
                cid = xml_escape(calls[0].call_id)
                for kind in ("dtmf", "sendDtmf", "playDtmf"):
                    code, rb = req(kind, x(kind, f" <callId>{cid}</callId>\r\n <tone>1</tone>\r\n"))
                    out(f"{code}  {kind} '1' valódi hívásra  {mask(rb)[:100]}")
                    if code == 200:
                        out("    -> hallottad a hangot a mobilon? (írd meg)")
                        break
                code, before = req("status", x("status", " <type>audioProperties</type>\r\n"))
                out(f"{code}  audioProperties előtte: {mask(before)[:200]}")
                for value in ("true", "false"):
                    code, rb = req("audioProperties", x("audioProperties", f" <mute>{value}</mute>\r\n"))
                    out(f"{code}  audioProperties mute={value}  {mask(rb)[:100]}")
                    time.sleep(1.5)
                code, after = req("status", x("status", " <type>audioProperties</type>\r\n"))
                out(f"{code}  audioProperties utána: {mask(after)[:200]}")
                out("Ellenőrizd a Briában, hogy a némítás ki van-e kapcsolva, majd bontsd a hívást.")
    finally:
        try:
            ws.close()
        except Exception:
            pass
        path = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "CallCompanion",
                            "bria_capabilities.txt")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        print(f"\nMentve: {path}")


if __name__ == "__main__":
    main()
