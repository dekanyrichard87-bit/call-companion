"""`python main.py --probe-bria [SECONDS]` - capture what Bria Enterprise writes during a test call.

Writes %LOCALAPPDATA%\\CallCompanion\\bria_probe.txt containing the candidate log files, every new log
line seen while the probe runs, and what the current parser made of them. The file holds phone
numbers / SIP data: review or redact it before sharing.
"""
import os
import time
from datetime import datetime

import config
from detectors import BriaLogDetector, LogTail, find_files


def run(cfg: dict, seconds: int = 90):
    bria = cfg["detectors"]["bria_log"]
    out_path = config.data_dir() / "bria_probe.txt"
    events = []
    parser = BriaLogDetector(bria, events.append)   # used only for its _feed(); not started
    started = time.time()
    tails: dict[str, LogTail] = {}

    with open(out_path, "w", encoding="utf-8") as out:
        out.write(f"Bria probe {datetime.now():%Y-%m-%d %H:%M:%S}\n== candidate files ==\n")
        files = find_files(bria["log_globs"])
        if not files:
            out.write("(none found - check detectors.bria_log.log_globs)\n")
        for p in files:
            st = os.stat(p)
            out.write(f"{p}  {st.st_size} bytes  modified {datetime.fromtimestamp(st.st_mtime):%H:%M:%S}\n")
        out.write("\n== new log lines ==\n")
        print(f"Probing {len(files)} file(s) for {seconds}s. Place / receive a test call NOW "
              "(ring, answer, hang up).")
        last_scan = 0.0
        while time.time() - started < seconds:
            if time.time() - last_scan > 2:
                last_scan = time.time()
                for p in find_files(bria["log_globs"]):
                    if p not in tails:
                        try:
                            new_file = os.path.getmtime(p) > started
                            tails[p] = LogTail(p, from_end=not new_file)
                        except OSError:
                            continue
            for p, t in tails.items():
                for line in t.read_lines():
                    out.write(f"[{os.path.basename(p)}] {line}\n")
                    parser._feed(line)
            out.flush()
            time.sleep(0.2)
        out.write("\n== parser events ==\n")
        for e in events:
            out.write(f"{e.kind}\t{e.number}\t{e.call_id}\n")
        if not events:
            out.write("(parser recognised no call - send this file so the parser can be adapted)\n")

    print(f"Parser recognised {len(events)} event(s): {[e.kind for e in events]}")
    print(f"Saved: {out_path}")
