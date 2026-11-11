"""Configuration: defaults + optional config.json next to the app (deep-merged)."""
import copy
import json
import os
import sys
from pathlib import Path

APP_NAME = "CallCompanion"

DEFAULTS = {
    "minicrm": {
        # true = no network calls, fake caller returned -> lets you test the UI first.
        "mock": True,
        # extension = cookies pushed by the Chrome extension (recommended)
        # browser   = read from Chrome/Edge/Firefox profile via browser_cookie3 (breaks on Chrome 127+)
        # manual    = paste a Cookie header into cookie_header (testing only)
        "cookie_source": "extension",
        "base_url": "https://r3.minicrm.hu/",
        "session_check_url": "https://r3.minicrm.hu/",
        # If the final URL / redirect Location contains this text, the session is treated as logged out.
        "login_url_hint": "login",
        "cookie_domain": "minicrm.hu",
        "browsers": ["chrome", "edge", "firefox"],
        "cookie_header": "",
        "search_url": "https://r3.minicrm.hu/System/Search?Query={term}",
        # {tail9} = last 9 digits, {local} = 06..., {e164} = +36..., {digits}
        "search_variants": ["{tail9}"],
        "parse": {
            # 1) page <title> like "Kovács István - MiniCRM" (generic titles are ignored)
            "title_regex": "^(.*?)\\s+[-–|]\\s+MiniCRM\\s*$",
            "title_generic": ["", "minicrm", "keresés", "search", "bejelentkezés",
                              "login", "főoldal", "dashboard"],
            # 2) fallback: result rows in the page (adjust after looking at last_lookup.html)
            "row": "table tr",
            "name": "a",
            "company": ".customer-company, .company-name",
            "link": "a[href]",
        },
        # Saves the last real response to %LOCALAPPDATA%\CallCompanion\last_lookup.html (contains
        # customer data - for tuning the selectors; turn off when done).
        "debug_dump": True,
        "timeout_s": 4,
    },
    "local_api": {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 48653,          # must match extension/background.js and manifest.json
        "extension_id": "",     # optional: pin to your extension id (chrome://extensions)
    },
    "detectors": {
        "bria_log": {
            "enabled": True,
            # Optional explicit log file (or list of files). When set, nothing is searched. Leave empty: the log is
            # found by itself under %APPDATA% (log_globs, then log_globs_fallback). Environment variables are expanded.
            "log_path": "",
            "log_globs": [
                # live log: ...\Bria Enterprise\<version>\<profile>\Logs\Log.txt, e.g.
                # ...\6.0\default_user\Logs\Log.txt and ...\6.0\104@dekorosmuvekbundle.hu\Logs\Log.txt.
                # The detector tails whichever of them was written to most recently.
                "%APPDATA%\\CounterPath Corporation\\Bria Enterprise\\*\\*\\Logs\\Log.txt",
            ],
            # Only used while log_globs matches no file at all (other Bria layouts); the newest match wins here too.
            "log_globs_fallback": [
                "%APPDATA%\\CounterPath Corporation\\Bria Enterprise\\*\\*\\Diagnostic Log\\*.log",
                "%APPDATA%\\CounterPath Corporation\\Bria Enterprise\\*\\*\\Logs\\*.log",
            ],
            # Bria Enterprise 6.x marks the SIP direction on the line before each message:
            #   "RESIP:TRANSPORT | ... incoming from: [ V4 ip:port UDP ... ]"  /  "... Transmitting to [ ... ]"
            "incoming_regex": "(?i)incoming from",
            "outgoing_regex": "(?i)Transmitting to",
        },
    },
    "ui": {
        "answered_fade_ms": 2500,
        "margin_px": 16,
        "end_linger_ms": 3000,                # v0.7.0: the ended card stays this long with the outcome, then fades
        # v0.5.1: where the call cards go (read by overlay.py)
        "overlay_position": "bottom_right",   # bottom_right | bottom_left | top_right | top_left
        "offset_x": -380,                     # px added to x (negative = left): clears Bria's incoming-call toast
        "offset_y": 0,                        # px added to y (negative = up)
        "dock_to_bria": False,                # True = sit left of Bria's toast when one is found (offsets otherwise)
        "bria_process_regex": "(?i)bria",     # Bria's program file name
        "dock_gap_px": 12,                    # gap between the toast and the docked cards
    },
    # v0.6.0: persistent call history in %LOCALAPPDATA%\CallCompanion\history.db (history.py)
    "history": {
        "retention_days": 90,           # older calls are deleted at start
        "max_rows": 2000,               # at most this many calls are kept
    },
    # Auto-update from GitHub Releases (updater.py). Only an installed copy (installer.iss) updates itself.
    "update": {
        "enabled": True,
        "repo": "",                     # owner/name; empty = version.GITHUB_REPO
        "include_prereleases": True,    # 0.x-beta releases are published as GitHub pre-releases
        "check_interval_h": 6,          # also re-check while running (0 = only at start)
        "startup_delay_s": 15,          # first check this long after start (keeps the start fast)
        "asset_regex": "^CallCompanion-Setup-.+\\.exe$",
        "require_checksum": True,       # refuse an installer without a SHA-256 (digest or .sha256 asset)
        "github_token": "",             # only for a private repository (read-only token)
        "timeout_s": 15,
    },
}


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def resource_path(rel: str) -> Path:
    """A file bundled with the app (assets/...): PyInstaller unpacks datas to sys._MEIPASS (the _internal folder)."""
    base = getattr(sys, "_MEIPASS", None)
    return (Path(base) if base else Path(__file__).resolve().parent) / rel


def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    p = Path(base) / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def _merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load() -> dict:
    """DEFAULTS <- config.json next to the exe (shipped, replaced by every update)
                <- %LOCALAPPDATA%\\CallCompanion\\config.json (optional per-user overrides, never touched by updates)"""
    cfg = copy.deepcopy(DEFAULTS)
    for path in (app_dir() / "config.json", data_dir() / "config.json"):
        if path.exists():
            with open(path, "r", encoding="utf-8-sig") as fh:     # -sig: Notepad may save with a BOM
                _merge(cfg, json.load(fh))
    return cfg
