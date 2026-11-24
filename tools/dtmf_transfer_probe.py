"""DTMF transfer probe v1 (preparation for 1.2.0): can Call Companion transfer a call with the PBX's in-call code?

Opennetworks set up in-call DTMF transfer (2026-11-24): #11 -> 101, #12 -> 102 ... Typed on Bria's in-call keypad
it works (verified 2026-11-24: Bria's Log.txt shows `url=tone:#;duration=200;oob`, `tone:1`, `tone:1`, then the PBX
sends BYE 0.44 s after the last digit). This probe sends the same code through the Bria Desktop API (GET /dtmf),
which answered 200 in the capabilities probe but was never verified to reach the PBX.

Diagnostic tool: not bundled in the exe / installer.
Run DURING an answered test call (e.g. your own mobile calling the company number, answered on your extension):
    cd d:\\callcompanion\\call-companion
    .venv\\Scripts\\python tools\\dtmf_transfer_probe.py
    (optional: --code "#11" --gap 0.25)

1. Lists the calls Bria reports; it needs exactly one call, answered and not on hold.
2. Asks for the code (default #11) and a confirmation: the call really IS transferred (that is the test).
3. Sends the code one tone at a time (GET /dtmf, <tone>X</tone>, `gap` seconds apart). UNVERIFIED: the spelling of
   '#' and '*' - if Bria answers 400 to '#', the variants 'pound' / 'hash' ('*': 'star') are tried and logged.
4. Watches for 8 s: does the call disappear from Bria (status call), and which `url=tone:` / BYE lines appear in
   Bria's Log.txt meanwhile (the same lines as with the keypad = the PBX got the code).
Everything is printed and saved to %LOCALAPPDATA%\\CallCompanion\\dtmf_transfer_probe.txt (send that file).
"""
import argparse
import glob
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

LINES = []
LOG_GLOB = os.path.expandvars(r"%APPDATA%\CounterPath Corporation\Bria Enterprise\*\*\Logs\Log.txt")
VARIANTS = {"#": ["#", "pound", "hash"], "*": ["*", "star"]}
RE_TONE = re.compile(r"MediaResourceParticipant playing.*?url=tone:([^;\s]+);")   # one line per tone
RE_BYE = re.compile(r"^(?:BYE|CANCEL) sip:")


def out(s=""):
    line = f"{datetime.now():%H:%M:%S.%f}"[:-3] + f"  {s}"
    print(line, flush=True)
    LINES.append(line)


def mask(s):
    return re.sub(r"\d(?=\d{3})", "#", re.sub(r"\s+", " ", s or ""))


def bria_log():
    files = [f for f in glob.glob(LOG_GLOB) if os.path.isfile(f)]
    return max(files, key=os.path.getmtime) if files else None


def read_from(path, pos):
    try:
        with open(path, "rb") as f:
            f.seek(pos)
            return f.read().decode("utf-8", "replace")
    except OSError as e:
        return f"(nem olvasható: {e})"


def save():
    folder = os.path.join(os.environ.get("LOCALAPPDATA") or ".", "CallCompanion")
    try:
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "dtmf_transfer_probe.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(LINES) + "\n")
        print(f"\nMentve: {path}  (ezt küldd el)")
    except OSError as e:
        print(f"\nNem sikerült menteni: {e}")


def main():
    ap = argparse.ArgumentParser(description="DTMF transfer probe")
    ap.add_argument("--code", default="", help='átkapcsolási kód, pl. "#11" (alap: rákérdez)')
    ap.add_argument("--gap", type=float, default=0.25, help="szünet a jelek között, mp (alap 0.25)")
    args = ap.parse_args()

    out("DTMF transfer probe v1")
    api = BriaApi({"timeout_s": 5}, "CallCompanion/dtmf-probe")
    try:
        ws = api._open()
    except BriaApiError as e:
        out(f"Nem sikerült kapcsolódni a Briához: {e.code} {e.detail}")
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
            ws = api._open()
            return f"ERR {e.code}", e.detail

    def calls():
        code, body = req("status", "<status>\r\n <type>call</type>\r\n</status>")
        if code != 200:
            out(f"status call -> {code} {mask(body)[:160]}")
            return None
        return parse_calls(body) if body.strip() else []

    try:
        cl = calls()
        if cl is None:
            return
        out(f"Hívások a Briában: {len(cl)}")
        for c in cl:
            out(f"  id={c.call_id} hold={c.hold or '-'} számok={[mask(n) for n in c.numbers]} "
                f"állapot={c.states}")
        if len(cl) != 1:
            out("Pontosan egy hívás kell (felvéve, nem tartva). Kilépek.")
            return
        call = cl[0]
        if call.held_locally:
            out("A hívás tartva van - tartásnál nem megy át a jel. Folytasd a hívást, és indítsd újra.")
            return

        code = args.code.strip() or input("\nÁtkapcsolási kód (Enter = #11): ").strip() or "#11"
        if not re.fullmatch(r"[0-9*#]{1,12}", code):
            out(f"Érvénytelen kód: {code!r} (csak 0-9, * és #).")
            return
        if input(f"A(z) {code} elküldése ÁTKAPCSOLJA a hívást. Mehet? [i/N]: ").strip().lower() not in (
                "i", "igen", "y"):
            out("Megszakítva.")
            return
        out(f"kód: {code}, szünet: {args.gap:g} s")

        log_path = bria_log()
        log_pos = os.path.getsize(log_path) if log_path else 0
        out(f"Bria napló: {log_path or 'NEM TALÁLHATÓ'}")

        cid = xml_escape(call.call_id)
        t0 = time.monotonic()
        for ch in code:
            sent = False
            for tone in VARIANTS.get(ch, [ch]):
                rc, rb = req("dtmf", f"<dtmf>\r\n <callId>{cid}</callId>\r\n <tone>{xml_escape(tone)}</tone>\r\n</dtmf>")
                out(f"{rc}  dtmf {tone!r}  {mask(rb)[:100]}")
                if rc == 200:
                    sent = True
                    break
            if not sent:
                out(f"A(z) {ch!r} jelet a Bria nem fogadta el - a próba itt megáll.")
                break
            time.sleep(args.gap)
        t_sent = time.monotonic()

        out("Figyelek 8 mp-ig: eltűnik-e a hívás a Briából...")
        gone_at = None
        while time.monotonic() - t_sent < 8:
            cl = calls()
            if cl is not None and not any(c.call_id == call.call_id for c in cl):
                gone_at = time.monotonic() - t_sent
                break
            time.sleep(0.3)
        out(f"A hívás {'véget ért %.2f s-mal az utolsó jel után' % gone_at if gone_at is not None else 'NEM ért véget 8 mp alatt'}"
            f" (küldés összesen {t_sent - t0:.2f} s)")

        if log_path:
            time.sleep(0.5)
            tones, byes = [], 0
            for ln in read_from(log_path, log_pos).splitlines():
                m = RE_TONE.search(ln)
                if m:
                    tones.append(m.group(1))
                    out(f"  napló: {ln.split('| CPCAPI2')[0].strip()[:160]}")
                elif RE_BYE.match(ln.strip()):
                    byes += 1
                    out(f"  napló: {ln.strip()[:60]}")
            out(f"Bria naplójában látott jelek: {''.join(tones) or '-'}, BYE/CANCEL: {byes}")
            if "".join(tones) == code:
                out("EREDMÉNY: a Bria ugyanúgy küldte a jeleket, mint a billentyűzetről"
                    + (" és a hívás véget ért -> az API-s átkapcsolás MŰKÖDIK." if gone_at is not None else
                       ", de a hívás nem ért véget -> kérdés a központnak (időzítés?)."))
            else:
                out("EREDMÉNY: a naplóban nem ugyanazok a jelek jelentek meg, mint a kód.")
        out("Kérlek, írd meg azt is: csörgött-e a célmelléken, és mit hallott a hívó.")
    finally:
        try:
            ws.close()
        except Exception:
            pass
        save()


if __name__ == "__main__":
    main()
