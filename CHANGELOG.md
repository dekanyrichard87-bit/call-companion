# Changelog

All notable changes to Call Companion. Format based on [Keep a Changelog](https://keepachangelog.com);
versions follow [Semantic Versioning](https://semver.org) with a pre-release label.

## [0.2.0-beta] - 2026-11-06

First end-to-end working version: a real Bria Enterprise call shows the caller's MiniCRM name.

### Added
- **Chrome MV3 extension** that relays the MiniCRM session cookies to the app (start-up, cookie changes, every minute),
  and a hardened **local API** on `127.0.0.1:48653` (`/ext/session`, `/ext/ping`, `/event`).
- **Bria Enterprise log detector** for `Logs\Log.txt`: ring, answer, hang-up, missed and rejected calls; direction
  from Bria's `incoming from` / `Transmitting to` markers; UTF-8/UTF-16 tailing; mid-call re-INVITEs ignored.
- **MiniCRM lookup through the full-search `Batch` request** (`POST /<system id>/Batch`), first `Person` item,
  with fallback `06...` -> `+36...` -> last 9 digits, per-number cache and profile link (`#AddressBook/<id>`).
- Number normalisation for PBX-prefixed numbers (`00036...` -> `+36...`).
- Diagnostics: one log line per MiniCRM attempt, optional response dumps (`debug_dump`), `--probe-bria` capture tool.
- Session health check with a "MiniCRM login needed" notice and a tray notice when the extension is not connected.
- Single-instance lock, per-user autostart (`--install-autostart`), PyInstaller spec and PowerShell install scripts
  (not yet verified on a clean machine).
- `--version`, `version.py`, README and this changelog.

### Changed
- Shipped `config.json` now uses real lookups (`mock: false`) and no dump files (`debug_dump: false`).
- Chrome extension version aligned with the app: `version` `0.2.0`, `version_name` `0.2.0-beta`.

### Fixed
- Pop-up crash on the second call (`Internal C++ object (CallOverlay) already deleted`): the controller now drops its
  reference when a card closes and checks the Qt object is still valid before using it.
- Outgoing calls no longer pop up as incoming.
- An error reply to a mid-call re-INVITE no longer closes the card.
- Numbers with extra leading zeros no longer become `+036...`.

### Known issues
See *Known limits and workarounds* in the README (cold-start session gap, ~1.2 s lookup, no company name,
"not found" cached until restart, packaging unverified).

## [0.1.0] - 2026-11-05 (proof of concept)

- PySide6 tray app with the dark frameless pop-up card (ringing, answered, ended states, action buttons).
- Mock MiniCRM lookup, simulated calls (`--simulate`), localhost test events.
- First Bria log parser and cookie reading from the browser profile (superseded by the extension in 0.2.0).
