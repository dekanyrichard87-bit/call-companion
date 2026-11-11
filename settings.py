"""Settings the user changes inside the app (v0.6.0): %LOCALAPPDATA%\\CallCompanion\\settings.json.

Separate from config.json on purpose: config.json is edited by hand (or shipped with the app), settings.json is
written only by the app itself (developer-mode switch, the saved extension list), so a save never rewrites a
hand-edited file and an update never touches either of them.

    {
      "dev_mode": false,
      "extensions": [ {"number": "102", "name": "Kovács Péter"}, ... ]
    }
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path

from PySide6.QtCore import QObject, Signal

log = logging.getLogger("callcompanion.settings")

MAX_EXTENSIONS = 40
_EXT_RE = re.compile(r"^[0-9*#+]{1,15}$")      # what a desk phone can dial: digits, * # and a leading +


def clean_extension(number: str, name: str):
    """(number, name) stripped and checked; None when the number is not dialable or the name is empty."""
    number = re.sub(r"[\s\-/().]", "", str(number or ""))
    name = " ".join(str(name or "").split())[:60]
    if not _EXT_RE.match(number) or not name:
        return None
    return number, name


class Settings(QObject):
    changed = Signal()          # any value changed (dev mode, extensions)

    def __init__(self, path: Path | str):
        super().__init__()
        self.path = Path(path)
        self._data = {"dev_mode": False, "extensions": []}
        try:
            if self.path.exists():
                with open(self.path, "r", encoding="utf-8-sig") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    self._data.update(data)
        except (OSError, ValueError):
            log.exception("settings.json unreadable - using defaults (the file is left as it is)")

    # ---- values -------------------------------------------------------------------------------------------------
    @property
    def dev_mode(self) -> bool:
        return bool(self._data.get("dev_mode", False))

    def set_dev_mode(self, on: bool) -> None:
        if bool(on) != self.dev_mode:
            self._data["dev_mode"] = bool(on)
            log.info("developer mode %s", "ON" if on else "off")
            self._save()

    @property
    def extensions(self) -> list[tuple[str, str]]:
        """[(number, name), ...] in the user's order, invalid entries skipped."""
        out = []
        for e in self._data.get("extensions") or []:
            if isinstance(e, dict):
                ok = clean_extension(e.get("number"), e.get("name"))
                if ok:
                    out.append(ok)
        return out[:MAX_EXTENSIONS]

    def set_extensions(self, items) -> None:
        clean, seen = [], set()
        for number, name in items:
            ok = clean_extension(number, name)
            if ok and ok[0] not in seen:
                seen.add(ok[0])
                clean.append({"number": ok[0], "name": ok[1]})
        self._data["extensions"] = clean[:MAX_EXTENSIONS]
        log.info("extension list saved: %d entr%s", len(clean), "y" if len(clean) == 1 else "ies")
        self._save()

    # ---- file ---------------------------------------------------------------------------------------------------
    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix="settings.", suffix=".tmp", dir=str(self.path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)          # atomic: a crash never leaves half a file
        except OSError:
            log.exception("settings.json could not be saved")
        self.changed.emit()
