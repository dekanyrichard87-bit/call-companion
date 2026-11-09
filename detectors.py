"""Bria log detector (tail + SIP reconstruction). Local API / extension relay live in local_api.py."""
import codecs
import collections
import glob
import logging
import os
import re
import threading
import time

from events import CallEvent, normalize_number

log = logging.getLogger(__name__)

START = re.compile(r"\b(INVITE|BYE|CANCEL)\s+(?:sips?|tel):|\bSIP/2\.0\s+(\d{3})")
RE_FROM = re.compile(r"(?i)\bFrom\s*:\s*(.*)")
RE_TO = re.compile(r"(?i)(?<![-\w])To\s*:\s*(.*)")            # not "Reply-To:" / "X-To:"
RE_REASON = re.compile(r"(?i)^\s*Reason\s*:\s*(.*)")
RE_CALLID = re.compile(r"(?i)\bCall-ID\s*:\s*(\S+)")
RE_CSEQ = re.compile(r"(?i)\bCSeq\s*:\s*\d+\s+(\w+)")
RE_USER = re.compile(r"(?i)(?:sips?|tel):([^@>;:\s]+)")


def _expand(pattern: str) -> str:
    """Expand %VARS% (and $VARS). %APPDATA% falls back to <home>\\AppData\\Roaming when the variable is not set."""
    pattern = str(pattern or "").strip()
    if "%APPDATA%" in pattern.upper() and not os.environ.get("APPDATA"):
        roaming = os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
        pattern = re.sub(r"(?i)%APPDATA%", lambda _m: roaming.replace("\\", "\\\\"), pattern)
    return os.path.expandvars(pattern)


def find_files(patterns) -> list[str]:
    if isinstance(patterns, str):
        patterns = [patterns]
    out = []
    for pat in patterns or []:
        if not str(pat or "").strip():
            continue
        for p in glob.glob(_expand(pat), recursive=True):
            if os.path.isfile(p):
                out.append(p)
    return out


class LogTail:
    """Binary tail with BOM-based encoding detection (UTF-8 / UTF-16), partial-line buffering and
    truncation handling. Opened with default share flags so it can read a file Bria is writing."""

    def __init__(self, path: str, from_end: bool = True):
        self.path = path
        with open(path, "rb") as f:      # opened only briefly: Bria can rotate/rename its log freely
            head = f.read(4)
        bom = 0
        if head[:2] == b"\xff\xfe":
            self.enc, bom = "utf-16-le", 2
        elif head[:2] == b"\xfe\xff":
            self.enc, bom = "utf-16-be", 2
        elif head[:3] == b"\xef\xbb\xbf":
            self.enc, bom = "utf-8", 3
        else:
            self.enc = "utf-8"
        self._bom = bom
        self._reset(os.path.getsize(path) if from_end else bom)

    def _reset(self, pos: int):
        if self.enc.startswith("utf-16"):
            pos -= pos % 2
        self.pos = max(pos, self._bom)
        self.dec = codecs.getincrementaldecoder(self.enc)(errors="replace")
        self.buf = ""

    def read_lines(self) -> list[str]:
        size = os.path.getsize(self.path)
        if size < self.pos:          # truncated / rewritten
            self._reset(0)
        if size == self.pos:
            return []
        with open(self.path, "rb") as f:  # no handle is kept between polls
            f.seek(self.pos)
            data = f.read(size - self.pos)
        self.pos += len(data)
        parts = (self.buf + self.dec.decode(data)).split("\n")
        self.buf = parts.pop()
        return [ln.rstrip("\r") for ln in parts]

    def close(self):
        """Nothing to release: the file is opened per read (kept so existing callers still work)."""
        pass


class BriaLogDetector(threading.Thread):
    """Tails the newest Bria diagnostics file and reconstructs call state from SIP messages.

    INVITE (incoming) -> ringing, 200 OK to INVITE -> answered, BYE/CANCEL/error -> ended.
    An outgoing INVITE (we transmit it) is reported the same way with direction "out" and the dialled number
    taken from its To header; 401/407 challenges to it are ignored (Bria re-sends the INVITE with credentials).
    The exact layout of Bria Enterprise's log is unverified: run `--probe-bria`, place a test call
    and check that the parser output lists ringing/answered/ended.
    """

    def __init__(self, cfg: dict, emit):
        super().__init__(daemon=True, name="bria-log")
        self.cfg, self.emit = cfg, emit
        self._stop_evt = threading.Event()
        self.calls: dict[str, str] = {}
        self.dirs: dict[str, str] = {}      # call id -> "in" | "out"
        self.answered: set[str] = set()
        self._blk = None
        self._warned_fallback = False
        self.path = None        # log file currently tailed (read by main.Controller._bria_log_path)
        self._ctx = collections.deque(maxlen=2)
        self._in = re.compile(cfg["incoming_regex"])
        self._out = re.compile(cfg["outgoing_regex"])

    def stop(self):
        self._stop_evt.set()

    @staticmethod
    def _newest_of(paths):
        best, mt = None, -1.0
        for p in paths:
            try:
                m = os.path.getmtime(p)
            except OSError:
                continue
            if m > mt:
                best, mt = p, m
        return best

    def _newest(self):
        """The log file to tail. Order: explicit `log_path` (nothing is searched), then the newest file matching
        `log_globs`, then - only if that matches nothing - the newest match of `log_globs_fallback`.
        The user-specific account folder (e.g. 104@company.hu) is a wildcard, so no path is hardcoded."""
        explicit = self.cfg.get("log_path")
        if explicit:
            return self._newest_of(find_files(explicit))
        found = self._newest_of(find_files(self.cfg.get("log_globs")))
        if found:
            return found
        found = self._newest_of(find_files(self.cfg.get("log_globs_fallback")))
        if found and not self._warned_fallback:
            self._warned_fallback = True
            log.warning("no file matches detectors.bria_log.log_globs; using fallback match %s", found)
        return found

    def run(self):
        path, tail, last_scan, idle = None, None, 0.0, 0
        while not self._stop_evt.is_set():
            now = time.time()
            if tail is None or now - last_scan > 5:
                last_scan = now
                newest = self._newest()
                if newest and newest != path:
                    try:
                        new_tail = LogTail(newest, from_end=True)
                    except OSError as e:
                        log.warning("cannot open %s: %s", newest, e)
                        new_tail = None
                    if new_tail:
                        if tail:
                            tail.close()
                        tail, path = new_tail, newest
                        self.path = path
                        log.info("Tailing Bria log: %s (%s)", path, tail.enc)
            if tail is None:
                time.sleep(1)
                continue
            try:
                lines = tail.read_lines()
            except OSError:
                lines = []
            for line in lines:
                try:
                    self._feed(line)
                except Exception:
                    log.exception("log parse error")
            if not lines:
                idle += 1
                if idle == 2:            # quiet for ~0.2 s
                    try:
                        self.flush_idle()
                    except Exception:
                        log.exception("log parse error")
                time.sleep(0.1)
            else:
                idle = 0

    def _feed(self, line: str):
        m = START.search(line)
        if m:
            self._flush_cancel()          # the previous message (a CANCEL waiting for a Reason header) is complete
            window = line + " " + " ".join(self._ctx)
            outgoing = bool(self._out.search(window)) and not self._in.search(window)
            self._blk = {
                "method": m.group(1), "status": int(m.group(2)) if m.group(2) else None,
                "from": None, "to": None, "callid": None, "cseq": None, "reason": "",
                "incoming": not outgoing, "done": False, "n": 0,
            }
        elif self._blk:
            b = self._blk
            b["n"] += 1
            if b["n"] > 60:
                self._flush_cancel()
                self._blk = None
            else:
                if not b["from"] and (mm := RE_FROM.search(line)):
                    u = RE_USER.search(mm.group(1))
                    if u:
                        b["from"] = u.group(1)
                if not b["to"] and (mm := RE_TO.search(line)):
                    u = RE_USER.search(mm.group(1))
                    if u:
                        b["to"] = u.group(1)
                if not b["reason"] and (mm := RE_REASON.search(line)):
                    b["reason"] = mm.group(1).lower()
                if not b["callid"] and (mm := RE_CALLID.search(line)):
                    b["callid"] = mm.group(1)
                if not b["cseq"] and (mm := RE_CSEQ.search(line)):
                    b["cseq"] = mm.group(1).upper()
                self._maybe_emit()
                if not line.strip():      # end of the headers
                    self._flush_cancel()
        self._ctx.append(line)

    def _flush_cancel(self):
        """A CANCEL is reported only once its headers are complete, because a `Reason:` header after the
        Call-ID tells "caller hung up" (missed) from "another phone answered" (not missed)."""
        b = self._blk
        if not b or b["done"] or b["method"] != "CANCEL" or not b["callid"]:
            return
        b["done"] = True
        cid = b["callid"]
        if cid in self.calls:
            elsewhere = "elsewhere" in b["reason"] or "cause=200" in b["reason"]
            self._end(cid, "elsewhere" if elsewhere else "cancel")

    def flush_idle(self):
        """Called when the log has been quiet for a moment: a CANCEL at the very end of the log is reported."""
        self._flush_cancel()

    def _end(self, cid: str, reason: str):
        number, direction = self.calls.pop(cid), self.dirs.pop(cid, "in")
        self.answered.discard(cid)
        self.emit(CallEvent("ended", cid, number, "bria-log", direction, reason))

    def _maybe_emit(self):
        b = self._blk
        if not b or b["done"] or not b["callid"]:
            return
        cid, meth, st = b["callid"], b["method"], b["status"]
        if meth == "INVITE":
            party = b["from"] if b["incoming"] else b["to"]      # the caller, or the number we dial
            if not party:
                return                                           # header not seen yet: look again on the next line
            if cid not in self.calls:                            # a re-INVITE (hold, auth retry) keeps the call
                number = normalize_number(party)
                direction = "in" if b["incoming"] else "out"
                self.calls[cid], self.dirs[cid] = number, direction
                self.emit(CallEvent("ringing", cid, number, "bria-log", direction))
        elif meth == "CANCEL":
            return                       # reported by _flush_cancel() when the headers are complete
        elif meth == "BYE":
            if cid in self.calls:
                self._end(cid, "bye")
        elif st is not None:
            if not b["cseq"]:
                return
            if b["cseq"] == "INVITE" and cid in self.calls:
                if st in (401, 407) and self.dirs.get(cid) == "out":
                    return  # auth challenge to our INVITE: Bria re-sends it with credentials, same Call-ID
                if st == 200:
                    if cid not in self.answered:
                        self.answered.add(cid)
                        self.emit(CallEvent("answered", cid, self.calls[cid], "bria-log", self.dirs.get(cid, "in")))
                elif st >= 300 and cid not in self.answered:
                    # call rejected / failed before answer. After answer, an error reply to a
                    # mid-call re-INVITE (hold, refresh) must NOT end the call; BYE does that.
                    self._end(cid, "rejected" if self.dirs.get(cid) == "in" else "failed")
                else:
                    return  # 1xx provisional: keep waiting
        b["done"] = True
