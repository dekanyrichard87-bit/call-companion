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
    reason: str = ""        # only on "ended": "cancel" | "bye" | "rejected" | "failed" | "elsewhere" | "" (unknown)


def normalize_number(raw: str) -> str:
    """Normalize Hungarian-style numbers to +36...; leave short extensions untouched."""
    raw = raw or ""
    s = re.sub(r"[^\d+]", "", raw)
    if not re.search(r"\d", s):
        return raw.strip()
    if s.startswith("+"):
        return s
    if s.startswith("00"):
        # international prefix; the PBX may add extra zeros (0036..., 00036...) -> drop all of them
        rest = s.lstrip("0")
        return "+" + rest if rest else s
    if s.startswith("06"):
        return "+36" + s[2:]
    return s
