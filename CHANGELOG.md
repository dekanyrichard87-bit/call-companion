# Changelog

All notable changes to Call Companion. Format based on [Keep a Changelog](https://keepachangelog.com);
versions follow [Semantic Versioning](https://semver.org) with a pre-release label.

## [0.4.0-beta] - 2026-11-09

Call life-cycle release: the card now follows the whole call, missed calls are not lost, outgoing calls are covered,
and the Chrome extension finds the app faster after a start.

### Added
- **Call duration counter**: an answered call keeps its card open and shows a live counter in the header
  (`00:15`, `01:42`, `1:02:03`). The card shows name, company, number and the buttons during the call and fades out
  when the call ends. A company that arrives during the call is shown.
- **Persistent missed-call cards**: a call that the caller cancels before you answer turns its card into a
  **MISSED CALL · hh:mm** card that stays until you close it (**✕**) or open the contact / search Google (**Copy** keeps
  it). Several cards are stacked in the bottom-right corner: the live call at the bottom, missed calls above it, newest
  nearest. A repeated missed call from the same number replaces the older card and counts up (`×2`). A lookup or
  company that arrives after the caller hung up still fills the card. Not counted as missed: calls you declined,
  calls answered on another phone (`Reason: ... completed elsewhere` on the CANCEL) and failed calls.
- **Outgoing calls**: the detector reports an outgoing INVITE (the number comes from its `To` header; 401/407 challenges
  are ignored so the card does not flicker) and the controller shows the same card with **OUTGOING CALL**, the MiniCRM
  lookup (name and company) for the dialled number, the duration counter once the other side answers, and a fade-out at
  the end. Unanswered outgoing calls never become missed-call cards. The status window's call table marks them with an
  arrow and the outcome *Nem fogadták* (not answered).
- **Faster extension sync after a start** (`extension/background.js`, `content.js`): after a failed push the extension
  retries every 2 s for 2 minutes instead of waiting for the 30 s alarm, and the content script on an open MiniCRM tab
  pokes the worker every 5 s while the app is not known to be up. The alarm retry stays as the fallback. The push
  reason shows in the log: `extension push (retry-fast|poke): N cookies`.
- `CallEvent` has two new optional fields: `direction` (`"in"` default, `"out"`) and `reason` (on `ended`: `cancel`,
  `bye`, `rejected`, `failed`, `elsewhere`). `StatusModel.call_ringing(call_id, number, direction="in")` and
  `StatusModel.call_ended(call_id, rejected=False, outcome=None)` got optional parameters; `CallOverlay.show_ringing`
  takes `direction`, and `CallOverlay` has `set_missed()`, `elapsed_s()` and the `number`, `is_missed`,
  `missed_count` properties. `BriaLogDetector.flush_idle()` reports a CANCEL that ends the log.
- Local API: `/event?...&dir=out` and `&reason=...` (see the README). Simulator scenarios *Kimenő hívás* and *Kimenő,
  nem fogadják*, and the tray entry **Test outgoing call**.
- New optional `ui` keys, read by `main.py` (not added to `config.py`): `missed_persistent` (default `true`),
  `missed_max` (default `4`) and `missed_close_on_action` (default `true`).

- **Bria log path auto-detection** (`config.py`, `detectors.py`): without an explicit path the log is searched under
  `%APPDATA%` with wildcards for the user-specific profile folder and the most recently written file is tailed.
  New optional keys under `detectors.bria_log`: `log_path` (exact file or list; wins over any search; empty = auto) and
  `log_globs_fallback` (default `...\*\*\Diagnostic Log\*.log` and `...\*\*\Logs\*.log`, used only while `log_globs`
  matches no file; logged as a warning once). `log_globs` and its default are unchanged. `%APPDATA%` falls back to
  `<home>\AppData\Roaming` when the variable is missing. The fallback layouts are not verified against a real Bria.
- **Startup-folder autostart** (`install.ps1`, `uninstall.ps1`): `install.ps1` creates `Call Companion.lnk` in the
  user's Startup folder, removes an older `Run` registry entry (no double start) and prints a deployment check
  (program, shortcut, no fixed paths in `config.json`, Bria log found). `uninstall.ps1` removes the shortcut too.
  `--install-autostart` / `--remove-autostart` (registry `Run` key) are unchanged. Not run on a real Windows PC yet.

### Removed
- **Logged-in user name** (introduced in 0.3.0-beta): identifying the logged-in MiniCRM user per workstation turned out
  to be unreliable and brings nothing to the call card. Gone: the `Rpc/Profile/<id>` request, its cache and the config
  keys `user_id`, `user_request`, `user_name_path` and `user_lookup` (`crm.py`, `main.py`); `StatusModel.set_user_name` /
  `user_name` (`status_window.py`), whose header is now the static text "Call Companion Aktív" (the MiniCRM state stays in its row); the profile
  page scraping, `#Profile` hash detection, avatar scan and `webRequest` capture in the extension; the `userName`,
  `userId` and `userIdCandidates` fields of the extension push (an older extension that still sends them is
  harmless, the app ignores them); the log lines `user id=...` and `session cookie names`. `local_api.py` needed no
  change. A leftover `user_id` in an existing `config.json` is ignored.

### Changed
- An answered call no longer shrinks to a compact view and fades after 2.5 s. `ui.answered_fade_ms` is therefore **no
  longer used** (still accepted in `config.json`).
- The simulator's *answered* scenario now lasts 20 s of talk time (was 4 s) so the counter is visible; the missed
  scenario ends with a CANCEL and leaves a missed-call card.
- A CANCEL is reported when its headers are complete (blank line, next message, or 0.2 s of quiet), so a `Reason`
  header after the Call-ID can be read.
- Extension version `0.4.0` (was `0.3.0`); reload the extension in `chrome://extensions` after updating. Its permissions shrink to `cookies` and `alarms`
  (`storage` is no longer needed).

### Known issues
- Outgoing-call detection and the "answered elsewhere" rule were built from the SIP standard and the log markers
  already seen, not from a captured outgoing call or a ring-group call. Check with `--probe-bria` (README, limits).
- Only one live call card at a time: a second call replaces the first one's card; missed-call cards are kept.
- If the hang-up of an answered call is missing from the Bria log, the counter keeps running until you close the card.
- The extension still finds a late-starting app only within 2 minutes (every 2 s), then every 5 s while a MiniCRM tab is
  open, otherwise at the 30 s alarm.

## [0.3.1-beta] - 2026-11-09

Callers with many deal cards in MiniCRM now resolve correctly, the card shows the caller's **company**, and the lookup
copes with callers that have two profiles under one number. Plus the fixes found while preparing the live test.

### Added
- **Company name on the card (stage 2)**: the name still appears first (about 360 ms); a second request then fetches the
  contact's detail record (`Rpc/Account/MainInfo/<id>` and `Rpc/Account/DetailedInfo/<id>` in one `Batch` POST, about
  120 ms) and the company pops in on the card's second line. It runs on a worker thread; a failure is only logged and the
  card keeps the name. A late company is dropped when the call was answered, ended or replaced by a newer call.
  The company is read from `Body > BusinessRelated > Name` (confirmed on the live system); without `company_path` the
  shallowest field called `BusinessRelated`, `CompanyName`, `Company`, `BusinessName`, `Business`, `Organization`,
  `Organisation` or `Employer` is used, and the first company wins. It is shown exactly as stored in MiniCRM.
- **Duplicate `Person` profiles** (for example a private `@gmail.com` and a business `@feszultseg.hu` profile of the same
  caller): the name and MiniCRM button still come from the first hit; stage 2 asks the shown profile first and then the
  other `Person` items of the same search, in order, at most 3 profiles per call, and uses the first company found.
- **New `CallerInfo` fields and methods** (`crm.py`): `company`, `alternates` (list of `(name, profile_url)` of the other
  `Person` items) and the `company_name` property; `MiniCrmClient.fetch_company(info)` and `company_enabled()`;
  `CallOverlay.set_company(company)`; the `company_sig` signal in `main.py`'s `Controller`.
- New optional `minicrm` config keys, read by `crm.py` (not added to `config.py`): `company_lookup` (default `true`),
  `company_request` (the detail request's form data, `{id}` = contact id; **stage 2 stays off while it is empty**),
  `company_path` (optional exact location of the company, `>`-separated, `{id}` allowed) and `company_same_name`
  (default `true`: only profiles with the same name as the shown caller may supply the company).
- New log lines: `person item fields=[...] company=from <Field>|none persons=N`, `company id=<id> -> found=True from
  BusinessRelated (N ms)`, `company id=<id> -> none (N ms) fields=[...]` (field paths only), `company id=<id> -> ...
  (0 ms, cached)`, `company: candidate K of N had it (id=<id>)`, `company: skipped N candidate(s) with a different name
  (company_same_name)` and `company id=<id> fetch failed: ...`. Names and company names are never logged there.
- **Person-only search**: the search request sends `Requests[0][Params][Type] = "Person"` (what MiniCRM's *Személy*
  filter sends), so a number tied to dozens of deals ("Adatlap") still answers on page 1.
- **Paging safety net**: if MiniCRM returns non-Person items despite the filter, or the filter is switched off, the
  remaining result pages are fetched in parallel (up to `search_max_pages`) until a person is found. A failed page is
  reported as an error, never as "unknown".
- **Deal-subtext fallback**: when no person exists under any number spelling, the contact name is read from the sub-line
  of the deal cards (`Kis István, 06305256058`), but only if all matching cards name the same person. The MiniCRM
  button then opens the deal. Costs one extra request for callers unknown to MiniCRM.
- New optional `minicrm` config keys, read by `crm.py` (not added to `config.py`): `search_type` (default `"Person"`,
  `""` = no type filter) and `search_max_pages` (default `8`, range 1-20).
- New log lines: `type filter '...' not applied by MiniCRM (got {...})`, `paging term=...: N page(s) (...)`,
  `deal fallback: N non-person item(s), M with the caller's number, K distinct name(s) -> used|ambiguous|none`,
  and `lookup ... (N ms, cached)` for cache hits.

### Changed
- **Card layout** (`overlay.py`): top to bottom **name** (22 px, bold), **company** (15 px, semi-bold; hidden when there
  is none) and **phone number** (12 px, muted); without a company the number sits directly below the name. Before the
  lookup answers, the number is the big line with "Looking up…" below it. An unknown caller now reads **Ismeretlen
  hívó** (was "Unknown caller") with the number below; a failed lookup keeps the number as the big line and shows
  "Lookup failed" plus the reason. The compact in-call view keeps name and number. The labels `lbl_name`, `lbl_company`
  and `lbl_number` keep their names; only their order on the card changed.
- **Result cache with expiry**: found names are kept for 12 hours, "not found" for 5 minutes, errors are never cached
  (`FOUND_TTL_S` / `NOT_FOUND_TTL_S` in `crm.py`). Companies are cached per contact id (12 hours when found, 5 minutes
  when the contact has none: `COMPANY_TTL_S`). A cached lookup keeps its alternates. A cache hit now reports its own (near zero) time instead of the time
  of the original lookup.
- **Attempt log line format**: it now shows `type=` and `page=`, and `types=` is a count over all items
  (`{'Person': 1}`) instead of a list of the first six. It also lists the response's `body keys`, which shows whether
  MiniCRM sends a page or total count.
- `debug_dump` files are named `last_lookup_<attempt>_p<page>.json` (for example `last_lookup_1of3_p1.json`,
  `last_lookup_deals_p1.json`); `last_lookup.json` is still the most recent one.
- The status window's minimum height is 520 px (was 640), so the footer buttons stay on screen on small displays.
- `version.py` now reads `0.3.1-beta`. The Chrome extension is unchanged and stays at `0.3.0`.
- README: lookup section (tiers, company stage 2, duplicate profiles), card layout, known limits, config reference
  (company keys and the `company_request` snippet) and troubleshooting table updated; the privacy note now says that
  the `lookup ...` log line contains the resolved caller name and that the `person item` / `company` lines contain
  only ids, field names and counts.

### Fixed
- **A number with many deal cards showed "Unknown caller"** (real case: 130 results over 6 pages, the person on page 6),
  because only page 1 was read and only for the first `Person`.
- An "unknown" result stayed cached until the app was restarted; it now expires after 5 minutes.
- **Re-check hint**: without a Chrome extension, the "open Chrome with MiniCRM" hint is no longer overwritten by the short
  status text, and the button stays busy for at least 0.7 s so a near-instant answer still looks like a registered click.
- **Bria log**: the log file is opened only for each read instead of being held open, so Bria can rotate or rename it.
  The Bria health row is now yellow ("Nem található Bria naplófájl (log_globs?)") when no log file is found instead of
  green. Both changes are untested against a live Bria.
- A malformed extension push (`cookies` not a list) no longer raises inside the controller.

### Known issues
- Stage 2 needs `company_request` in `config.json`; the field name `BusinessRelated` is confirmed for this MiniCRM
  system only. If a contact shows no company, the `company id=... -> none ... fields=[...]` log line shows where the data
  is (set `company_path` accordingly).
- A company is shown only from a profile with the **same name** as the caller (`company_same_name`); a business profile
  saved under a differently spelled name (for example without the accents) is skipped. Set the key to `false` to accept
  any profile with the number.
- The deal-subtext fallback assumes the sub-line is in the result's `Content` field; this has not been checked against a
  real response yet. The check is the `deal fallback: ...` log line.
- If MiniCRM sends no page count and the type filter stops working, paging fetches up to `search_max_pages` pages.

## [0.3.0-beta] - 2026-11-07

Operations release: a status window, a warmer MiniCRM connection and a more resilient Chrome extension.
Prepared for the first live test with Bria Enterprise and the MiniCRM Chrome extension on the office PC.

### Added
- **Status window** (`status_window.py`): dark window opened by double-clicking the tray icon, the tray menu entry
  *Status window* or `python main.py --show`. Shows the health of the Bria log watcher, the MiniCRM session, the
  Chrome extension and the local API, a table of recent calls (double-click a row to open the contact in MiniCRM),
  a **call simulator** (missed / answered / long ring) that runs through the real pipeline, and a button that opens
  the log folder. The interface text is Hungarian.
- **Window placement**: the status window opens in the bottom-right corner of the usable screen area, just above the
  taskbar and clock. If Windows reports no tray icon position, the screen under the mouse cursor is used.
- **Re-check feedback**: the *MiniCRM újraellenőrzése* button greys out while the check runs and a result line shows
  the outcome. Without an active Chrome extension it says so at once; if no answer arrives within 8 s it reports a
  timeout and re-enables the button.
- **MiniCRM connection warm-up** (`crm.py`): a light `HEAD` request about every 30 s keeps one HTTPS connection open
  so the lookup at ring time can reuse it. Optional `minicrm` keys: `warmup` (default `true`) and
  `warmup_interval_s` (default `30`, minimum `10`). The lookup log line now shows `warm=True/False`.
- **Extension retry and heartbeat**: while the app is not reachable the extension retries every 15-30 s; a heartbeat
  push every 5 minutes keeps the app's view fresh (the status window flags the extension after 12 min of silence).
- **Logged-in user name**: a content script (`extension/content.js`) reads the display name from the MiniCRM page and
  the status window greets the user with it. The selectors are guesses until checked on the real page.
- `BriaLogDetector.path`: the log file currently tailed, so the status window can report a missing or stale log.
- `--show` command line flag.

### Changed
- `version.py` now reads `0.3.0-beta` (it had stayed at `0.2.0-beta` while the extension manifest was already `0.3.0`).
- Extension permissions: `cookies`, `alarms`, `storage`; host permissions `https://r3.minicrm.hu/*` and
  `http://127.0.0.1/*`.
- README updated to match: heartbeat and retry behaviour, status window, new log lines, warm-up keys.

### Fixed
- The Bria health row in the status window never showed "log not found" or "log not changing", because the detector
  kept the log path in a local variable only.

### Known issues
See *Known limits and workarounds* in the README. Still open: packaging not verified on a clean machine, no company
name on the card, "not found" cached until restart, `content.js` selectors unverified, legacy config keys and unused
packages (`beautifulsoup4`, `lxml`) not yet removed, no automated tests.

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
