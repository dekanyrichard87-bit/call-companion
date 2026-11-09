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
            "log_globs": [
                # live log: ...\Bria Enterprise\<version>\<profile>\Logs\Log.txt, e.g.
                # ...\6.0\default_user\Logs\Log.txt and ...\6.0\104@dekorosmuvekbundle.hu\Logs\Log.txt.
                # The detector tails whichever of them was written to most recently.
                "%APPDATA%\\CounterPath Corporation\\Bria Enterprise\\*\\*\\Logs\\Log.txt",
            ],
            # Bria Enterprise 6.x marks the SIP direction on the line before each message:
            #   "RESIP:TRANSPORT | ... incoming from: [ V4 ip:port UDP ... ]"  /  "... Transmitting to [ ... ]"
            "incoming_regex": "(?i)incoming from",
            "outgoing_regex": "(?i)Transmitting to",
        },
    },
    "ui": {"answered_fade_ms": 2500, "margin_px": 16},
}


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


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
    cfg = copy.deepcopy(DEFAULTS)
    path = app_dir() / "config.json"
    if path.exists():
        with open(path, "r", encoding="utf-8") as fh:
            _merge(cfg, json.load(fh))
    return cfg
