"""Auto-updater: GitHub Releases -> download the installer -> run it silently -> the app restarts.

How it works
  1. Shortly after start (and then every `update.check_interval_h` hours) a background thread asks the GitHub
     Releases API of `update.repo` (default: version.GITHUB_REPO) for its releases. Drafts are skipped,
     pre-releases are included unless `update.include_prereleases` is false (every 0.x-beta is a pre-release).
  2. The highest SemVer tag that is newer than version.__version__ and carries an installer asset
     (`CallCompanion-Setup-<version>.exe`) wins.
  3. The installer is downloaded to %LOCALAPPDATA%\\CallCompanion\\updates and its SHA-256 is checked against the
     asset's GitHub digest or the `<installer>.sha256` asset that build.ps1 uploads next to it.
  4. As soon as no call is ringing / running and no missed-call card is waiting, the installer is started with
     /VERYSILENT and the app quits. The installer (installer.iss) closes any leftover process, replaces the files
     in place (per-user install, no UAC prompt), keeps config.json and starts the new version again.

Only an installed copy updates itself (the frozen exe with unins000.exe next to it). From a source checkout or
an unpacked dist folder the check runs and is logged, but nothing is downloaded or replaced.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests
from PySide6.QtCore import QObject, QTimer, Signal

import config
from version import GITHUB_REPO, __version__

log = logging.getLogger("callcompanion.updater")

API_RELEASES = "https://api.github.com/repos/{repo}/releases?per_page=30"
MAX_INSTALLER_BYTES = 400 * 1024 * 1024      # sanity limit for a download
RETRY_BUSY_MS = 30_000                       # re-try applying while a call / missed card is on screen
MAX_ATTEMPTS = 2                             # installs of the same version per 24 h before giving up


class UpdateError(Exception):
    pass


# ---- SemVer ------------------------------------------------------------------------------------------------
_SEMVER = re.compile(r"^[vV]?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")


def version_key(v: str):
    """Sort key following SemVer 2.0 precedence, or None if `v` is not a version (e.g. a non-release tag).
    1.0.0-alpha < 1.0.0-alpha.1 < 1.0.0-beta < 1.0.0-beta.2 < 1.0.0-beta.11 < 1.0.0-rc.1 < 1.0.0"""
    m = _SEMVER.match((v or "").strip())
    if not m:
        return None
    major, minor, patch, pre = m.groups()
    if pre is None:
        pre_key = (1,)                                   # a release sorts after all of its pre-releases
    else:
        pre_key = (0, tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split(".")))
    return int(major), int(minor), int(patch), pre_key


def is_newer(candidate: str, current: str = __version__) -> bool:
    a, b = version_key(candidate), version_key(current)
    return a is not None and b is not None and a > b


# ---- GitHub ------------------------------------------------------------------------------------------------
@dataclass
class Release:
    version: str              # "0.5.1-beta" (tag without the leading v)
    tag: str                  # "v0.5.1-beta"
    page_url: str             # release page on github.com
    prerelease: bool
    asset_name: str           # CallCompanion-Setup-0.5.1-beta.exe
    asset_url: str            # download URL
    asset_size: int
    sha256: str | None        # from the asset's GitHub digest, if present
    sha256_url: str | None    # URL of the <asset>.sha256 asset, if present


def _settings(cfg: dict) -> dict:
    u = dict(cfg.get("update") or {})
    u.setdefault("enabled", True)
    u["repo"] = u.get("repo") or GITHUB_REPO
    u.setdefault("include_prereleases", True)
    u.setdefault("check_interval_h", 6)
    u.setdefault("startup_delay_s", 15)
    u.setdefault("asset_regex", r"^CallCompanion-Setup-.+\.exe$")
    u.setdefault("require_checksum", True)
    u.setdefault("github_token", "")
    u.setdefault("timeout_s", 15)
    return u


def _headers(s: dict, accept: str = "application/vnd.github+json") -> dict:
    h = {"Accept": accept, "User-Agent": f"CallCompanion/{__version__}", "X-GitHub-Api-Version": "2022-11-28"}
    if s.get("github_token"):
        h["Authorization"] = f"Bearer {s['github_token']}"
    return h


def find_latest(s: dict) -> Release | None:
    """The newest release with an installer asset (newer or not), or None if the repo has none."""
    r = requests.get(API_RELEASES.format(repo=s["repo"]), headers=_headers(s), timeout=s["timeout_s"])
    if r.status_code == 404:
        raise UpdateError(f"repository {s['repo']} not found (private? set update.github_token)")
    if r.status_code == 403 and r.headers.get("X-RateLimit-Remaining") == "0":
        raise UpdateError("GitHub API rate limit reached, will retry later")
    r.raise_for_status()
    pattern = re.compile(s["asset_regex"], re.IGNORECASE)
    best: Release | None = None
    for rel in r.json():
        if rel.get("draft") or (rel.get("prerelease") and not s["include_prereleases"]):
            continue
        tag = rel.get("tag_name") or ""
        if version_key(tag) is None:
            continue
        assets = rel.get("assets") or []
        inst = next((a for a in assets if pattern.match(a.get("name", ""))), None)
        if inst is None:
            continue
        side = next((a for a in assets if a.get("name", "").lower() == inst["name"].lower() + ".sha256"), None)
        digest = inst.get("digest") or ""
        # With a token (private repo) the browser URL does not work; the API URL + octet-stream does.
        url = inst["url"] if s.get("github_token") else inst["browser_download_url"]
        side_url = (side["url"] if s.get("github_token") else side["browser_download_url"]) if side else None
        cand = Release(
            version=tag.lstrip("vV"), tag=tag, page_url=rel.get("html_url", ""),
            prerelease=bool(rel.get("prerelease")), asset_name=inst["name"], asset_url=url,
            asset_size=int(inst.get("size") or 0),
            sha256=digest.split(":", 1)[1].lower() if digest.lower().startswith("sha256:") else None,
            sha256_url=side_url)
        if best is None or version_key(cand.tag) > version_key(best.tag):
            best = cand
    return best


# ---- download ----------------------------------------------------------------------------------------------
def updates_dir() -> Path:
    p = config.data_dir() / "updates"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _expected_hash(rel: Release, s: dict) -> str | None:
    if rel.sha256:
        return rel.sha256
    if rel.sha256_url:
        r = requests.get(rel.sha256_url, headers=_headers(s, "application/octet-stream"), timeout=s["timeout_s"])
        r.raise_for_status()
        m = re.search(r"\b([0-9a-fA-F]{64})\b", r.text)
        if m:
            return m.group(1).lower()
    return None


def download(rel: Release, s: dict) -> Path:
    expected = _expected_hash(rel, s)
    if expected is None and s["require_checksum"]:
        raise UpdateError(f"{rel.tag}: no SHA-256 for {rel.asset_name} (upload {rel.asset_name}.sha256)")
    target = updates_dir() / rel.asset_name
    if target.exists() and expected and _sha256_file(target) == expected:
        log.info("update %s already downloaded", rel.version)
        return target
    part = target.with_name(target.name + ".part")
    t0 = time.time()
    h, size = hashlib.sha256(), 0
    with requests.get(rel.asset_url, headers=_headers(s, "application/octet-stream"), stream=True,
                      timeout=(s["timeout_s"], 120)) as r:
        r.raise_for_status()
        with open(part, "wb") as fh:
            for chunk in r.iter_content(256 * 1024):
                size += len(chunk)
                if size > MAX_INSTALLER_BYTES:
                    raise UpdateError("download larger than expected, aborted")
                fh.write(chunk)
                h.update(chunk)
    digest = h.hexdigest()
    if expected and digest != expected:
        part.unlink(missing_ok=True)
        raise UpdateError(f"checksum mismatch for {rel.asset_name}: got {digest[:12]}…, expected {expected[:12]}…")
    if rel.asset_size and size != rel.asset_size:
        part.unlink(missing_ok=True)
        raise UpdateError(f"size mismatch for {rel.asset_name}: {size} != {rel.asset_size}")
    os.replace(part, target)
    log.info("update %s downloaded (%.1f MB in %.1f s, sha256 %s)", rel.version, size / 1e6, time.time() - t0,
             "verified" if expected else "NOT checked")
    return target


# ---- install -----------------------------------------------------------------------------------------------
def is_installed_copy() -> bool:
    """True for the exe installed by installer.iss (frozen, Inno Setup uninstaller next to it)."""
    return bool(getattr(sys, "frozen", False)) and (Path(sys.executable).parent / "unins000.exe").exists()


def _state_path() -> Path:
    return updates_dir() / "state.json"


def _load_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict):
    try:
        _state_path().write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        log.warning("could not write %s", _state_path())


def cleanup():
    """At start: forget a finished update and delete installers that are not newer than this version."""
    state = _load_state()
    if state.get("version") and not is_newer(state["version"]):
        log.info("update to %s completed (running %s)", state["version"], __version__)
        _state_path().unlink(missing_ok=True)
    for f in updates_dir().glob("CallCompanion-Setup-*"):
        m = re.match(r"CallCompanion-Setup-(.+?)\.exe(\.part)?$", f.name, re.IGNORECASE)
        if m is None or m.group(2) or not is_newer(m.group(1)):
            f.unlink(missing_ok=True)


def launch_installer(installer: Path) -> None:
    """Start the installer detached and silent. The caller must quit the app right after this."""
    log_file = config.data_dir() / "update-install.log"
    args = [str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", f"/LOG={log_file}"]
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(args, cwd=str(installer.parent), creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log.info("installer started: %s", " ".join(args))


# ---- Qt glue -----------------------------------------------------------------------------------------------
class UpdateManager(QObject):
    """Runs the checks on a worker thread and applies the update on the GUI thread when the app is idle.

    is_busy()  -> True while a call is ringing/running or a missed-call card waits (the update must not kill it)
    quit_app() -> shuts the app down cleanly (the installer starts the new version)"""
    found_sig = Signal(object, object)     # Release, Path | None   (worker thread -> GUI thread)
    message = Signal(str, str)             # tray balloon: title, text

    def __init__(self, cfg: dict, is_busy: Callable[[], bool], quit_app: Callable[[], None], parent=None):
        super().__init__(parent)
        self.s = _settings(cfg)
        self.is_busy = is_busy
        self.quit_app = quit_app
        self.state_text = "Not checked yet"
        self._checking = False
        self._pending: tuple[Release, Path] | None = None
        self._busy_timer = QTimer(self)
        self._busy_timer.setSingleShot(True)
        self._busy_timer.timeout.connect(self._try_apply)
        self._periodic = QTimer(self)
        self._periodic.timeout.connect(lambda: self.check(manual=False))
        self.found_sig.connect(self._on_found)

    def start(self):
        try:
            cleanup()
        except OSError as e:
            log.warning("update cleanup: %s", e)
        if not self.s["enabled"]:
            self.state_text = "Automatic updates are off (config.json)"
            log.info("auto-update disabled")
            return
        QTimer.singleShot(int(self.s["startup_delay_s"] * 1000), lambda: self.check(manual=False))
        hours = float(self.s["check_interval_h"] or 0)
        if hours > 0:
            self._periodic.start(int(hours * 3600 * 1000))

    # -- checking (worker thread) --
    def check(self, manual: bool = False):
        if self._checking:
            if manual:
                self.message.emit("Call Companion", "An update check is already running.")
            return
        if self._pending is not None:
            self._try_apply(manual)
            return
        self._checking = True
        threading.Thread(target=self._worker, args=(manual,), name="updater", daemon=True).start()

    def _worker(self, manual: bool):
        try:
            rel = find_latest(self.s)
            if rel is None or not is_newer(rel.version):
                self.state_text = f"Up to date ({__version__})"
                log.info("update check: up to date (%s, latest release %s)", __version__,
                         rel.version if rel else "none")
                if manual:
                    self.message.emit("Call Companion", f"You have the latest version ({__version__}).")
                return
            log.info("update check: %s available (running %s)", rel.version, __version__)
            if not is_installed_copy():
                self.state_text = f"{rel.version} available (not an installed copy, not applied)"
                log.info("not an installed copy (source run or unpacked dist) - update not applied")
                if manual:
                    self.message.emit("Call Companion", f"Version {rel.version} is available. This copy is not "
                                                        f"installed with the installer, so it is not updated.")
                return
            if manual:
                self.message.emit("Call Companion", f"Downloading version {rel.version}…")
            self.state_text = f"Downloading {rel.version}…"
            path = download(rel, self.s)
            self.found_sig.emit(rel, path)
        except (requests.RequestException, UpdateError, OSError, ValueError) as e:
            self.state_text = f"Update check failed: {e}"
            log.warning("update check failed: %s", e)
            if manual:
                self.message.emit("Call Companion – update", f"Update check failed: {e}")
        except Exception:                       # never let the updater take the app down
            self.state_text = "Update check failed (see app.log)"
            log.exception("update check crashed")
        finally:
            self._checking = False

    # -- applying (GUI thread) --
    def _on_found(self, rel: Release, path: Path):
        self._pending = (rel, path)
        self.state_text = f"{rel.version} ready, installs when no call is active"
        self._try_apply()

    def _try_apply(self, manual: bool = False):
        if self._pending is None:
            return
        if self.is_busy():
            if manual:
                self.message.emit("Call Companion", "The update installs after the current call "
                                                    "(and after the missed-call cards are closed).")
            self._busy_timer.start(RETRY_BUSY_MS)
            return
        rel, path = self._pending
        state = _load_state()
        recent = time.time() - float(state.get("last", 0)) < 24 * 3600
        attempts = int(state.get("attempts", 0)) if state.get("version") == rel.version and recent else 0
        if attempts >= MAX_ATTEMPTS:
            self._pending = None
            self.state_text = f"Installing {rel.version} failed {attempts}× – see update-install.log"
            log.error("update %s was already tried %d times in 24 h - giving up (see update-install.log)",
                      rel.version, attempts)
            self.message.emit("Call Companion – update",
                              f"Version {rel.version} could not be installed automatically. "
                              f"Please run the installer from GitHub by hand.")
            return
        if not path.exists():
            self._pending = None
            log.warning("downloaded installer vanished: %s", path)
            return
        _save_state({"version": rel.version, "attempts": attempts + 1, "last": time.time()})
        try:
            launch_installer(path)
        except OSError as e:
            self._pending = None
            self.state_text = f"Could not start the installer: {e}"
            log.error("could not start installer %s: %s", path, e)
            return
        log.info("quitting for the update to %s", rel.version)
        self.quit_app()


def check_once(cfg: dict) -> str:
    """For `main.py --check-update`: a one-line report, nothing is downloaded."""
    s = _settings(cfg)
    try:
        rel = find_latest(s)
    except (requests.RequestException, UpdateError) as e:
        return f"Update check failed: {e}"
    if rel is None:
        return f"No release with an installer in {s['repo']} (running {__version__})."
    verdict = "NEWER - would be installed" if is_newer(rel.version) else "not newer"
    hashed = "sha256 digest" if rel.sha256 else ("sha256 file" if rel.sha256_url else "NO checksum")
    return (f"Running {__version__}; latest release {rel.tag} ({'pre-release' if rel.prerelease else 'release'}, "
            f"{rel.asset_name}, {hashed}): {verdict}. Installed copy: {is_installed_copy()}.")
