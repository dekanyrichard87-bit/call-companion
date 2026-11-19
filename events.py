"""Call event model shared by all detectors."""
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class CallEvent:
    kind: str          # "ringing" | "answered" | "ended"   ("ringing" also means "dialling" when direction is "out")
    call_id: str
    number: str = ""   # normalized: the CALLER for direction "in", the DIALLED number for "out"
    source: str = ""
    direction: str = "in"   # "in" | "out"
    reason: str = ""        # only on "ended": "cancel" | "bye" | "rejected" | "failed" | "elsewhere" | "forwarded" | ""


# v1.1.0: caller with a withheld number (CLIR). Bria logs the caller as "000anonymous@<pbx>" (seen live 2026-11-18,
# Bria toast "anonymous / 000anonymous@192.168.21.79"); the old normalize kept only the digits -> "000".
ANONYMOUS = "anonymous"
ANONYMOUS_LABEL = "Rejtett szám"
_ANON_WORDS = re.compile(r"(?i)anonymous|restricted|private|unavailable|unknown|withheld|clir")


def is_anonymous(number: str) -> bool:
    """True for the ANONYMOUS marker and for history rows saved before 1.1.0 as only zeros ("000")."""
    s = str(number or "").strip()
    return s == ANONYMOUS or (s != "" and set(s) == {"0"})


def normalize_number(raw: str) -> str:
    """Normalize Hungarian-style numbers to +36...; leave short extensions untouched.
    06301234567, 0036301234567, 00036301234567 (PBX prefix), 006301234567 (outside-line 0 + 06...) -> +36301234567
    A withheld number (anonymous, restricted, private... with or without the PBX's leading zeros, or only zeros)
    -> ANONYMOUS."""
    raw = raw or ""
    if _ANON_WORDS.search(raw):
        return ANONYMOUS
    s = re.sub(r"[^\d+]", "", raw)
    if not re.search(r"\d", s):
        return raw.strip()
    if set(s.lstrip("+")) == {"0"}:            # only zeros: no real number
        return ANONYMOUS
    if s.startswith("+"):
        return s
    if re.fullmatch(r"006\d{8,9}", s):
        # v0.10.0-beta.2.1: the PBX's outside-line "0" in front of a national 06... number (Bria logs a call dialled as
        # 06301234567 as 006301234567, seen live 2026-11-16) -> +36... Not an international 006x...: a Hungarian
        # national number is 06 + 8 or 9 digits, so with the extra 0 it has 11-12 digits (an international number
        # 00 6x ... is longer, except some New Zealand landlines, accepted)
        return "+36" + s[3:]
    if s.startswith("00"):
        # international prefix; the PBX may add extra zeros (0036..., 00036...) -> drop all of them
        rest = s.lstrip("0")
        return "+" + rest if rest else s
    if s.startswith("06"):
        return "+36" + s[2:]
    return s
