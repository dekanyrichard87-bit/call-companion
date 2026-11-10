# Call Companion

**Version 0.5.1-beta** · Windows 10/11 x64 · one-click installer with automatic updates (built with Python 3.11+ / PySide6)

Call Companion is a small tray application that watches the **Bria Enterprise** softphone and, the moment a
call rings, shows a dark, frameless pop-up card near the bottom-right corner of the screen, just left of Bria's own
call notification. The card shows the
caller's number immediately and, a moment later, the caller's **name from MiniCRM** and, a little later still, the
caller's **company**, with one-click buttons to open the contact in MiniCRM, search the number on Google, or copy it.
While you talk, the card stays open and counts the call duration; a **missed call** stays on screen until you close
it; and **outgoing calls** get the same card and lookup for the number you dial. A **status window** (double-click the tray
icon) shows whether every part is healthy and lists the recent calls.

It needs **no API keys and no separate MiniCRM login**: it reuses the MiniCRM session of the Chrome profile you
are already logged in with, delivered by a tiny Chrome extension.

On a user's PC it is a single **`CallCompanion-Setup-<version>.exe`**: no Python, no admin rights, autostart at logon,
an entry in *Settings > Apps* for uninstalling, and **automatic updates** from this repository's GitHub Releases.

- [How it works](#how-it-works)
- [What works in this release](#what-works-in-this-release)
- [Known limits and workarounds](#known-limits-and-workarounds)
- [Installing on a PC (setup .exe)](#installing-on-a-pc-setup-exe)
- [Building the installer and publishing updates](#building-the-installer-and-publishing-updates)
- [Setup and first run](#setup-and-first-run)
- [Configuration reference](#configuration-reference)
- [Logs and troubleshooting](#logs-and-troubleshooting)
- [Security and privacy](#security-and-privacy)
- [Roadmap](#roadmap)
- [Project layout](#project-layout)
- [Versioning and releases](#versioning-and-releases)

---

## How it works

```
 Chrome (logged in to MiniCRM)                              Bria Enterprise
   extension/background.js                                    writes ...\Bria Enterprise\6.0\<profile>\Logs\Log.txt
   extension/content.js (wake-up poke)                                  |
   cookies of *.minicrm.hu                                              |  tailed every 0.1 s
        |  POST 127.0.0.1:48653/ext/session                              |
        v                                                                v
   local_api.LocalApi                                       detectors.BriaLogDetector
        |  session cookies                                      |  CallEvent: ringing / answered / ended
        v                                                       v
   crm.MiniCrmClient  <---- lookup(number) ----  main.Controller (Qt main thread)
        |                                                       |            |
        |  POST https://r3.minicrm.hu/60171/Batch               |            |  health, call log
        v                                                       v            v
   MiniCRM  --> name + profile link  ---------------->  overlay.CallOverlay   status_window.StatusWindow
                                                        (pop-up card)         (fed by StatusModel)
```

**Life of an incoming call**

1. Bria logs the SIP `INVITE`. `BriaLogDetector` reads the caller from the `From:` header, normalises it
   (`00036301234567` -> `+36301234567`) and emits a `ringing` event.
2. The controller shows the card **at once** with the number and "Looking up...".
3. A background thread asks MiniCRM for that number (see [lookup](#minicrm-lookup)), normally in about 360 ms. The card
   updates with the **name** and enables the MiniCRM button. Repeat callers are answered from an in-memory cache.
4. **Stage 2, in the background:** if the name is known but no company came with it, a second request fetches the
   contact's detail record (about 120 ms) and the **company** appears on the card's second line. The name never waits
   for it. If the request fails or finds no company, nothing changes on the card.
5. When you answer, the card stays open as an **IN CALL** card with a live duration counter (`00:15`, `01:42`, then
   `1:02:03`) and keeps the name, company, number and buttons. When the call ends (`BYE`) the card fades out and is
   destroyed.
6. If the caller gives up before you answer (`CANCEL`), the card turns into a **MISSED CALL** card that stays until
   you close it (see below). Calls you decline, calls answered on another phone (`Reason: ... completed elsewhere`)
   and calls that fail do not count as missed: their card fades out as before.
7. An **outgoing call** (you dial in Bria) shows the same card with **OUTGOING CALL**, looks up the *dialled* number the
   same way, switches to the duration counter when the other side answers and fades out when the call ends. An
   unanswered outgoing call never becomes a missed-call card.

**The card, top to bottom**

```
 ● INCOMING CALL                                  ✕     (OUTGOING CALL / IN CALL 01:42 / MISSED CALL ×2 · 08:41)
 Neupert Márk                  line 1  name: large, bold
 Dekoros Művek Kft.            line 2  company: medium (hidden when MiniCRM has none)
 +36 30 525 6058               line 3  phone number: small, muted
 [ 🔍 MiniCRM ] [ 🌐 Google ] [ 📋 Copy ]
```

**Missed-call cards.** A missed call stays on screen **indefinitely** until you close it with **✕**, or open the contact
(**MiniCRM**) or search the number (**Google**), which counts as dealt with. **Copy** keeps the card. Several cards can
be on screen: the live call (or a notice) is at the bottom, the missed-call cards are stacked above it, the newest
nearest to the bottom. A repeated missed call from the same number replaces the older card and counts up (`×2`, `×3`).
At most 4 missed-call cards are kept (the oldest is closed to make room). The name and company of a missed call are
filled in even if the lookup finishes after the caller hung up.

Before the lookup answers, the number is the big line and "Looking up…" sits below it. An unknown caller shows
**Ismeretlen hívó** with the number below; a failed lookup shows the number, "Lookup failed" and the reason (it is not
shown as unknown, because the caller is not known to be unknown).

Every call is also recorded in the status window's call table (time, number, name, state).

**Components**

| File | Role |
|---|---|
| `main.py` | Entry point, tray icon and menu, `Controller` (glues events, lookup, card and status together), autostart |
| `detectors.py` | `BriaLogDetector`: tails Bria's `Log.txt` and rebuilds call state (incoming and outgoing, with the end reason) from the SIP messages in it |
| `events.py` | `CallEvent` (kind, call id, number, source, `direction` in/out, end `reason`) and `normalize_number` |
| `local_api.py` | Local HTTP server on `127.0.0.1:48653` for the extension (`/ext/session`, `/ext/ping`) and test events (`/event`) |
| `crm.py` | `MiniCrmClient`: session cookies, MiniCRM lookup (name, then company), duplicate-profile handling, caching, connection warm-up, diagnostics |
| `overlay.py` | `CallOverlay`: the frameless dark card (name / company / number lines), states (ringing, outgoing, in call with counter, missed), card stacking, buttons, fade animations |
| `status_window.py` | `StatusModel` (data) and `StatusWindow` (dark window: health, call table, simulator, re-check) |
| `extension/` | Chrome MV3 extension: `background.js` relays the MiniCRM session cookies, `content.js` pokes the worker so the sync is fast |
| `probe.py` | `--probe-bria`: captures what Bria writes during a test call (for adapting the parser) |
| `updater.py` | Auto-update: checks GitHub Releases, downloads and verifies the installer, runs it silently when no call is active |
| `config.py` / `config.json` | Defaults, the shipped config and optional per-user overrides (deep-merged) |
| `version.py` | Version number and update repository (single source of truth for the app, the exe and the installer) |
| `build.ps1`, `callcompanion.spec`, `installer.iss`, `assets/` | Build: PyInstaller exe, Inno Setup installer (autostart, uninstaller), app icon |

### MiniCRM lookup

MiniCRM's web interface is a single-page app, so there is no page to download and parse. The app sends the same
background request the MiniCRM search page sends:

```
POST https://r3.minicrm.hu/60171/Batch    (form data: Rpc/List, Page.Search.Index, Query=<number>, Type=Person, Page=0)
-> {"Rpc/List": {"Status": 200, "Body": {"Items": [{"Type": "Person", "Name": "...", "Url": "#AddressBook/<id>"}]}}}
```

`Type=Person` is what the search page sends when you pick the **Személy** filter: MiniCRM then returns person cards
only. Without it, a number that is tied to dozens of deals (`Adatlap`) fills the first result pages with deals, and
the person card can sit on page 6 (a real case: 130 results, the person on page 6).

The caller is resolved in three tiers, cheapest first:

1. **Person search** (the normal case, one request). Page 1 of the `Type=Person` search; the **first `Person` with a
   name** wins: its `Name` is shown and its `Url` becomes the MiniCRM button. The other `Person` items of the same
   result are kept as *alternates* (see [duplicate profiles](#duplicate-profiles-and-the-company)). The number is searched in this order,
   stopping at the first hit:

   | Hungarian number | Other numbers |
   |---|---|
   | 1. `06301234567` | 1. `+4915123456789` |
   | 2. `+36301234567` (sent as `%2B...`) | 2. `4915123456789` |
   | 3. last 9 digits (`301234567`) | 3. last 9 digits |

2. **Paging (safety net).** If MiniCRM returns non-Person items despite the filter, or `search_type` is empty, a
   warning is logged and the remaining result pages are fetched **in parallel** (at most `search_max_pages`) until a
   person turns up. The number of pages comes from a count field in the response when there is one; otherwise a full
   page is assumed to have followers. A failed page is reported as a lookup error, never as "unknown".
3. **Deal subtext (last resort).** If no person exists under any spelling, one unfiltered request looks at the deal
   cards. Their sub-line reads like `Kis István, 06305256058`; the name is the segment just before the one that
   holds the caller's number. It is used **only when all matching cards name the same person**: several different
   names (for example a switchboard number) count as ambiguous and the caller stays unknown, because a wrong name on
   the card is worse than none. The MiniCRM button then opens the deal, not a person. This costs one extra request
   for callers that MiniCRM does not know at all.

Numbers shorter than 6 digits (internal extensions, "Anonymous") are not looked up. Failures are shown on the card
("Lookup failed" plus the reason) instead of a misleading "Ismeretlen hívó" (unknown caller).

**Result cache** (in memory): a found name is kept for 12 hours, "not found" for 5 minutes (so a contact you just
added is picked up without restarting the app), and errors are never cached. The cache entry keeps the alternates too.
The times are constants in `crm.py` (`FOUND_TTL_S`, `NOT_FOUND_TTL_S`, `COMPANY_TTL_S`), not config keys.

### Company name (stage 2)

MiniCRM's search result holds only name, e-mail and phone, so the company comes from the contact's **detail record**,
the same background request MiniCRM's own contact page sends when you open `#AddressBook/<id>`:

```
POST https://r3.minicrm.hu/60171/Batch    (two requests in one: Rpc/Account/MainInfo/<id> and Rpc/Account/DetailedInfo/<id>)
-> {"Rpc/Account/MainInfo/<id>": {"Status": 200, "Body": {"BusinessRelated": {"Name": "Dekoros Művek Kft."}, ...}}, ...}
```

- The company is read from `Body > BusinessRelated > Name`. Without a `company_path` the app searches the answer for
  the shallowest field called `BusinessRelated`, `CompanyName`, `Company`, `BusinessName`, `Business`, `Organization`,
  `Organisation` or `Employer` (an object's `Name`, or a list's first entry). **The first company wins** when there
  are several, and it is shown **exactly as stored in MiniCRM**.
- The name is shown first (about 360 ms) and the company follows when the detail request answers. It runs on a worker
  thread, so a slow or failing request never delays or breaks the name.
- It is **off until `company_request` is set** in `config.json` (the request is MiniCRM specific; see
  [Company name settings](#company-name-settings-stage-2)). If the search result itself already carries a company, no
  second request is made.
- A found company is cached per contact id for 12 hours; "no company" is re-checked after 5 minutes; failures are
  never cached.

#### Duplicate profiles and the company

One caller can exist as two `Person` profiles under the same number, for example a private profile (`@gmail.com`)
and a business profile (`@feszultseg.hu`). Only the business one carries the company. The app handles it like this:

1. The **name and the MiniCRM button come from the first hit**, exactly as before, so the card is as fast as ever.
2. In stage 2 the company is looked up on the shown profile first. If that profile has none, the **other `Person`
   items of the same search** are tried in search order, one detail request each, **at most 3 profiles per call**
   (`COMPANY_MAX_CANDIDATES` in `crm.py`). The first company found wins.
3. **Same-name guard** (`company_same_name`, default `true`): only alternates with the **same name** (case and spacing
   ignored, accents not) are tried. A different person who happens to share the number (a colleague, a relative) never
   lends their company to the card. Set it to `false` to accept any profile with that number.
4. If a request fails (network, expired session, HTTP error), the remaining candidates are not tried and nothing is
   cached, so the next call retries.

The MiniCRM button keeps pointing at the profile of the shown name, even when the company came from another profile.

---

## What works in this release

- **Caller resolution from MiniCRM** through the `Batch` search request with the Person-only filter (callers with
  dozens of deal cards still resolve on page 1), the three-step number fallback, the paging and deal-subtext
  fallbacks described above, and an in-memory cache with expiry.
- **Pop-up card** with live updates: ringing or dialling, in call (live duration), ended (fades out), missed (stays),
  and a notice card when the MiniCRM login has expired. Buttons: MiniCRM profile, Google search, copy number, close.
  The card sits above the taskbar, never steals keyboard focus, and survives back-to-back calls. Line order: **name**
  (large, bold), **company** (medium, hidden when there is none), **phone number** (small, muted).
- **Call duration counter**: an answered call keeps its card open with a live `mm:ss` counter until the call ends.
- **Persistent missed-call cards**: a missed call stays until you close it; stacked, counted per number, name and
  company filled in even if they arrive late (config: `ui.missed_persistent`, `ui.missed_max`, `ui.missed_close_on_action`).
- **Clear of Bria's notification** (0.5.1): the cards sit 380 px left of the bottom-right corner, so Bria Enterprise's
  incoming-call toast no longer covers them. Corner and offset are configurable (`ui.overlay_position`, `ui.offset_x`,
  `ui.offset_y`); optional `ui.dock_to_bria` places the cards directly beside the toast.
- **Outgoing calls**: the card and the MiniCRM lookup (name and company) also work for the number you dial from Bria.
- **Faster start-up sync**: the Chrome extension retries every 2 s for 2 minutes after the app was not reachable, and a
  MiniCRM tab pokes it every 5 s, so the session usually arrives within seconds of the app starting.
- **Company name in two stages**: the name appears first, the company follows from the contact's detail record
  (`BusinessRelated`), also for callers with a private and a business profile under one number (same-name guard).
  Optional and off until `company_request` is configured.
- **Status window**: opened by double-clicking the tray icon, the tray menu entry *Status window* or
  `python main.py --show`; the X button hides it to the tray. It opens in the bottom-right corner of the usable screen
  area, just above the taskbar and clock. It shows four health rows (Bria log, MiniCRM session, Chrome extension,
  local API), the last 50 calls (double-click a row to open the contact), a call simulator, a button to open the log
  folder, and the **MiniCRM újraellenőrzése** button. The interface text is Hungarian.
- **Re-check feedback**: the re-check button greys out while the check runs and a result line says what happened. Without
  an active Chrome extension it tells you to open Chrome with MiniCRM logged in; with no answer after 8 s it reports a
  timeout and re-enables the button.
- **Direct profile opening**: the MiniCRM button opens the contact (`.../60171/#AddressBook/<id>`) in the default browser.
- **Session sync without any login**: the Chrome extension pushes the MiniCRM cookies at browser start, on every
  cookie change and as a heartbeat every 5 minutes. If the app is not reachable it retries every 15-30 s until it is.
  The app warns when the session is missing or expired, and the status window flags the extension after 12 minutes
  without a push.
- **Connection warm-up**: a light request about every 30 s keeps one connection to MiniCRM open so the lookup at ring
  time does not pay for a new TLS handshake.
- **Bria Enterprise detection** from `Logs\Log.txt` (ring, answer, hang-up, missed call, rejected call; outgoing
  calls are ignored; mid-call re-INVITEs do not close the card).
- **Windows integration**: tray icon, single-instance lock, per-user autostart through a Startup-folder shortcut (no admin rights).
- **Installer and updates (0.5.0)**: one setup `.exe` (per-user, no UAC), Start menu entries, uninstaller in
  *Settings > Apps*, and automatic updates from GitHub Releases (SHA-256 verified, never during a call).
- **Diagnostics**: one log line per MiniCRM request (status, URL, page, item type counts, timing), optional raw response dumps,
  and `--probe-bria` to capture Bria's log format.

---

## Known limits and workarounds

| Limit | Why | Workaround / plan |
|---|---|---|
| **First call right after boot can still fail** with "no MiniCRM session yet" | The app starts before Chrome and the extension have delivered the cookies. Since 0.4.0 the extension retries every 2 s for 2 minutes (and a MiniCRM tab pokes it every 5 s), so this should take seconds, but it has not been timed on the office PC | Make sure Chrome (with MiniCRM logged in) is running before the first call; the status window shows when the session has arrived. Optional later: keep the last session encrypted per user |
| **Lookup time not re-measured with warm-up** | 0.2.0 measured about 1.2 s for the first lookup after a session sync (connection setup plus MiniCRM's search time). Warm-up should remove the connection part; the lookup log line now shows `warm=` and the time, but there is no finer breakdown yet | Check `app.log` during the live test. The number is shown instantly, only the name arrives later; repeat callers are cached |
| **Company needs a one-time config entry** | The detail request is MiniCRM specific, so stage 2 stays off until `company_request` is in `config.json` (the shipped `config.json` from 0.3.1-beta includes it) | See [Company name settings](#company-name-settings-stage-2). Without it the card simply has no company line |
| **Company field is an assumption for other accounts** | It was confirmed for this MiniCRM system (`BusinessRelated > Name`); another system may name the field differently | The `company id=... -> none ... fields=[...]` log line lists the answer's field paths (names only); set `company_path` to the right one |
| **Company arrives after the name** | A second request (about 120 ms) runs after the name is shown; it is dropped if the call was answered or ended meanwhile | Deliberate, so the name is never delayed |
| **A caller you just added to MiniCRM can show as unknown for up to 5 minutes** | "Not found" results are cached for 5 minutes (found names for 12 hours) | Wait 5 minutes, or quit and restart the app from the tray menu |
| **A name taken from a deal card is a fallback** | When no person card exists, the name comes from the deal's sub-line, and only if all matching cards agree. The layout of that sub-line has not been verified against a real response yet (it is assumed to be in the result's `Content` field) | If the name is missing or wrong, check the `deal fallback: ...` log line (below) and, if needed, set `debug_dump` and look at one dump |
| Only the **first matching person's name** is shown | Deliberate. Other `Person` items for the number are used only to find a company (same name only, at most 3) | - |
| A company is shown only if it is on a profile with the **same name** | `company_same_name` guards against showing a colleague's company | `"company_same_name": false` accepts any profile with that number |
| One **live** call card at a time | A new ringing call replaces the previous live card (a call that was answered or still ringing then has no card any more); missed-call cards are kept | - |
| **Outgoing calls are unverified against a real Bria log** | The outgoing INVITE layout (dialled number in the `To` header, 401/407 challenges) was built from the SIP standard and the `Transmitting to` marker, not from a captured call | Run `python main.py --probe-bria 90`, place an outgoing test call and check `Parser recognised ...: ['ringing','answered','ended']`; keep `bria_probe.txt` if not |
| **"Answered elsewhere" is unverified** | A call picked up on another phone is told apart from a missed call by the `Reason: ... completed elsewhere` header of the CANCEL; whether your PBX sends it is not known | If such calls leave a missed-call card, send the CANCEL lines from `--probe-bria` |
| **A lost hang-up leaves the in-call card open** | The card closes on the `BYE` found in the Bria log; if the log is rotated or the line is missed, the counter keeps running | Close it with **✕** |
| **Bria docking is unverified** | `ui.dock_to_bria` finds Bria's toast as a visible, always-on-top, toast-sized window of a process named `bria`; this has not been checked against a real Bria Enterprise toast yet, so it is off by default | Run `bria_toast_probe.py` during a test call; if the toast shows `topmost=True` and is under 640×420, set `"dock_to_bria": true` in the personal `config.json` |
| Cards follow the **primary screen** only | The cards and the Bria docking use the primary monitor | - |
| Missed-call cards: at most 4 | The stack must fit on the screen | `ui.missed_max` |
| Needs Bria **Log Level = Verbose** | At lower levels Bria writes no SIP messages | Help > Troubleshooting > Support. Verbose logs grow quickly. The app opens the log only for each read, so Bria can rotate or rename it; rotation has not been tested live |
| **Status window position is best effort** | Windows 11 often reports no position for tray icons, so the window goes to the bottom-right of the usable area of the screen under the cursor (or the primary screen) rather than exactly at the icon | - |
| **Stale-log warning after 30 min** | The Bria health row turns yellow when `Log.txt` has not changed for 30 minutes, which can also happen on a quiet line | Treat it as a hint, not an error |
| Chrome developer-mode extension | The extension is loaded unpacked, so Chrome shows a developer-mode warning at some start-ups | Planned: publish it unlisted on the Chrome Web Store |
| Hungarian-centric number handling | `+36`, `06`, `0036` and PBX-prefixed `000 36...` forms are normalised; other countries are searched as `+<digits>` | - |
| "Glass" effect is translucent only | No acrylic blur yet | - |
| **Installer not yet run on a clean machine** | `build.ps1` / `installer.iss` / `updater.py` were written and their logic unit-tested, but the first real build and the first update cycle happen on the colleague's PC test | Follow [the test plan](#first-test-on-a-colleagues-pc) |
| **Unsigned exe and installer** | No code-signing certificate. A setup downloaded with a browser gets a SmartScreen *Windows protected your PC* screen; antivirus may look harder at a new PyInstaller exe | *More info > Run anyway* once. Updates downloaded by the app itself carry no download mark, so they do not trigger SmartScreen. Code-signing is on the roadmap |
| **Update integrity = GitHub account** | The installer is checked against the SHA-256 published in the same release, which catches broken downloads but not a compromised GitHub account | Keep 2FA on the GitHub account; code-signing would add a real publisher check |
| No automated tests in the repository | The behaviour was verified with throw-away scripts (a fake MiniCRM server, a stand-in for Qt) and live calls | Planned |

Unused configuration keys `minicrm.search_url` and `minicrm.parse.*` are leftovers from an earlier HTML-parsing
approach and are ignored; `beautifulsoup4` and `lxml` are likewise no longer used by the lookup. They are still
present in this release and will be removed in a later one.

---

## Installing on a PC (setup .exe)

Nothing has to be installed beforehand: the setup contains the Python runtime, Qt and all assets.

1. Run **`CallCompanion-Setup-<version>.exe`** (from the GitHub release page or a USB stick). If SmartScreen says
   *Windows protected your PC*: **More info > Run anyway** (the file is not code-signed yet).
2. There are no questions: it installs for the current user to `%LOCALAPPDATA%\Programs\CallCompanion` (no admin
   rights), then the last page offers to start the app. The tray icon appears.
3. **Chrome extension, once**: `chrome://extensions` > **Developer mode** > **Load unpacked** > choose
   `%LOCALAPPDATA%\Programs\CallCompanion\extension` (Start menu: *Call Companion > Chrome extension folder* opens it).
   Log in to MiniCRM in that Chrome.
4. **Bria**: *Help > Troubleshooting > Support > Log Level = Verbose*, restart Bria.

What the installer sets up:

| | |
|---|---|
| Program | `%LOCALAPPDATA%\Programs\CallCompanion\CallCompanion.exe` (+ `_internal\`, `config.json`, `extension\`) |
| Autostart | `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Call Companion.lnk` (disable: Win+R `shell:startup`, delete it) |
| Start menu | *Call Companion* (opens the status window) and *Chrome extension folder* |
| Uninstall | *Settings > Apps > Installed apps > Call Companion > Uninstall* (or *Control Panel > Programs and Features*). Removes the program, the shortcuts and downloaded updates; keeps `%LOCALAPPDATA%\CallCompanion` (logs, personal config). Remove the Chrome extension by hand |
| Old autostart | The `HKCU\...\Run\CallCompanion` value from source installs is deleted, so the app never starts twice |

Silent install (e.g. remote): `CallCompanion-Setup-<version>.exe /VERYSILENT /SUPPRESSMSGBOXES`. Silent uninstall:
`"%LOCALAPPDATA%\Programs\CallCompanion\unins000.exe" /VERYSILENT`.

**Settings that survive updates**: every update replaces `config.json` next to the exe. Put personal changes in
`%LOCALAPPDATA%\CallCompanion\config.json` instead (same format, only the keys you change); it is merged on top and never
touched by the installer. Example, to turn automatic updates off on one PC: `{ "update": { "enabled": false } }`.

### Automatic updates

- About 15 s after start, and then every 6 hours, the app asks the GitHub Releases API of
  `dekanyrichard87-bit/call-companion` for the newest release. Pre-releases count (all `0.x-beta` versions are
  published as pre-releases); drafts do not.
- If the newest tag (`v0.5.1-beta`, SemVer order) is newer than the running version, the app downloads
  `CallCompanion-Setup-<version>.exe` in the background to `%LOCALAPPDATA%\CallCompanion\updates` and checks its
  SHA-256 (GitHub's asset digest or the `.sha256` file published next to it). A file that does not match is deleted.
- The update is applied only when **no call is ringing or running and no missed-call card is open**; otherwise it waits
  and retries every 30 s. Then the app starts the installer with `/VERYSILENT` and quits; the installer closes any
  leftover process, replaces the files, and starts the new version. A short tray message confirms it.
- If the same version fails to install twice within 24 hours, the app stops trying and shows a tray message; the
  installer's log is `%LOCALAPPDATA%\CallCompanion\update-install.log`.
- Tray menu **Check for updates** checks at once (and reports "latest version" / errors); its tooltip shows the last
  result. `CallCompanion.exe --check-update` writes a one-line report to `app.log` and exits.
- Only an installed copy updates itself. Running from source or from an unpacked `dist\CallCompanion` folder only logs
  that an update exists.

---

## Building the installer and publishing updates

On the **build PC** (yours) only:

- 64-bit Python 3.11+ with the `py` launcher
- [Inno Setup 6.3+](https://jrsoftware.org/isinfo.php): `winget install JRSoftware.InnoSetup`
- for publishing: GitHub CLI, logged in: `winget install GitHub.cli`, then `gh auth login`

```powershell
.\build.ps1             # -> dist\installer\CallCompanion-Setup-0.5.0-beta.exe (+ .sha256)
.\build.ps1 -Publish    # same, then creates GitHub release v0.5.0-beta with both files (pre-release for -beta)
.\build.ps1 -SkipInstaller   # only dist\CallCompanion\ (portable folder, no Inno Setup needed)
```

If scripts are blocked: `powershell -ExecutionPolicy Bypass -File .\build.ps1`. The script creates `.venv`, installs
`requirements.txt`, runs PyInstaller (`callcompanion.spec`: windowed one-folder build, icon, Windows version info,
`assets\` bundled), adds `config.json` and `extension\`, compiles `installer.iss` with the version from `version.py`, and
writes the SHA-256 file. `-Publish` refuses to run with uncommitted changes (`-Force` overrides) and takes the release
notes from the matching `CHANGELOG.md` section.

**Releasing an update** (the installed PCs pick it up by themselves):

1. Bump `__version__` in `version.py` (e.g. `0.5.1-beta`) and add a `CHANGELOG.md` section `## [0.5.1-beta] - date`.
2. Commit and push.
3. `.\build.ps1 -Publish`

Do not delete or re-tag a published release that PCs have already installed; publish a higher version instead.

### First test on a colleague's PC

1. On your PC: `.\build.ps1 -Publish` (creates release `v0.5.0-beta`).
2. On the colleague's PC: run the setup, load the Chrome extension, set Bria to Verbose (above).
3. Tray icon > **Status window**: Bria, local API, extension and MiniCRM rows should turn green; make one real call.
4. Update test: on your PC bump to `0.5.1-beta` (CHANGELOG entry), commit, `.\build.ps1 -Publish`. On the colleague's PC
   use tray > **Check for updates** (or wait up to 6 h / restart the app). After a few seconds the icon disappears and
   comes back, and the tray message says *Version 0.5.1-beta is running*.
5. If something fails, collect `%LOCALAPPDATA%\CallCompanion\app.log` and `update-install.log`.

---

## Setup and first run

### Requirements

- Windows 10 or 11, Python 3.11+ (for running from source)
- Bria Enterprise 6.x
- Google Chrome with the MiniCRM account logged in
- Packages: `PySide6`, `requests`, `beautifulsoup4`, `lxml` (see `requirements.txt`; `beautifulsoup4`/`lxml` are
  no longer used by the lookup and will be dropped)

### 1. Run from source

```powershell
py -3 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

The phone icon appears in the system tray. Hover it for the MiniCRM session status. **Double-click** it (or use the menu
entry **Status window**) to open the status window; `python main.py --show` opens it at start-up. Right-click menu:
the version, **Status window**, **Test incoming call**, **Test outgoing call**, **Re-check MiniCRM session**,
**Check for updates**, **Quit**. Check the version with
`python main.py --version`.

To try only the pop-up (no Bria, no MiniCRM) set `"mock": true` in `config.json` and run
`python main.py --simulate 06301234567`. The status window's simulator does the same without restarting.

### 2. Bria Enterprise

1. In Bria: **Help > Troubleshooting > Support > Log Level = Verbose**, then restart Bria.
2. Nothing needs configuring: the log is found by itself under `%APPDATA%`. The user-specific profile folder (for
   example `104@company.hu` or `default_user`) is a wildcard, and the **most recently written** file wins, so another
   colleague's account on another PC works without edits. Search order: (1) `detectors.bria_log.log_path`, if you set
   an exact file; (2) `log_globs`, default `%APPDATA%\CounterPath Corporation\Bria Enterprise\*\*\Logs\Log.txt`;
   (3) only when (2) matches nothing, `log_globs_fallback`, default `...\*\*\Diagnostic Log\*.log` and
   `...\*\*\Logs\*.log`. If the fallback is used the log says `no file matches ...log_globs; using fallback match <file>`.
   The fallback layouts are not verified against a real Bria: check with step 3.
3. Check it: `python main.py --probe-bria 90`, then ring, answer and hang up within 90 s. The last line must say
   `Parser recognised 3 event(s): ['ringing','answered','ended']`. If not, keep `%LOCALAPPDATA%\CallCompanion\bria_probe.txt`
   (it contains phone numbers; redact before sharing) so the parser can be adapted.
4. With the app running, `app.log` shows `Tailing Bria log: <path> (<encoding>)` and the status window's Bria row
   reads "Log.txt figyelése".

### 3. Chrome extension (pairing)

1. Open `chrome://extensions`, switch on **Developer mode**, click **Load unpacked**, choose the `extension` folder.
   After updating the app (0.4.0 changed the extension), click the reload icon on the extension card.
2. Log in to MiniCRM in **that Chrome profile**.
3. With the app running, `app.log` shows `extension push (...): N cookies` and the tray tooltip changes to
   *MiniCRM: logged in*; the status window's extension row shows the last sync time. A red `!` on the extension icon
   means the app is not reachable; the extension keeps retrying every 15-30 s.
4. Optional hardening: copy the extension's ID from `chrome://extensions` into `local_api.extension_id` so only
   that extension may talk to the app.

The extension needs the `cookies` and `alarms` permissions, host access to `https://r3.minicrm.hu/*`
(cookies, and a content script that only wakes the extension and reads nothing from the page) and to `http://127.0.0.1/*`. It talks only to
`127.0.0.1:48653` (the port must match `config.json` and `extension/background.js`).

### 4. Test without a real call

From the status window: enter a number, pick a scenario, click **Szimulálás**. Or from PowerShell:

```powershell
Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:48653/event?type=ring&number=%2B36301234567&id=t1"
Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:48653/event?type=answer&id=t1"
Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:48653/event?type=end&id=t1&reason=bye"
```

Optional parameters: `dir=out` makes it an outgoing call, and `reason=cancel|bye|rejected|failed|elsewhere` on `type=end`
says how the call ended (without `reason`, an unanswered incoming call counts as missed). The status window's simulator
also has *Kimenő hívás* (outgoing) scenarios, and the tray menu has **Test outgoing call**.

### 5. Standalone build

See [Building the installer and publishing updates](#building-the-installer-and-publishing-updates). For a source
checkout without the installer, `python main.py --install-autostart` / `--remove-autostart` use the per-user `Run`
registry key; the installer removes that key and uses a Startup-folder shortcut instead, so do not combine them.

---

## Configuration reference

`config.py` holds the defaults; `config.json` next to the app overrides them (nested keys are merged). The shipped
`config.json` sets `minicrm.mock = false` and `minicrm.debug_dump = false`.

**`minicrm`**

| Key | Default | Meaning |
|---|---|---|
| `mock` | `true` in code, `false` in shipped `config.json` | `true` = no network, fake caller (UI testing) |
| `cookie_source` | `"extension"` | `extension`, `browser` (reads the browser profile; breaks on Chrome 127+) or `manual` |
| `cookie_header` | `""` | Raw `Cookie` header for `manual` testing |
| `base_url` | `https://r3.minicrm.hu/` | MiniCRM host |
| `system_id` | `"60171"` (built into `crm.py`) | Your MiniCRM system id, the number in the URL |
| `batch_url` | built from the two above | Full override of the search request URL |
| `session_check_url` | `https://r3.minicrm.hu/` | Page used to test whether the session is logged in |
| `login_url_hint` | `"login"` | A redirect containing this text means "logged out" |
| `search_variants` | `["{tail9}"]` | Extra number spellings tried after `06...` and `+36...` (`{local}`, `{e164}`, `{digits}`, `{tail9}`) |
| `search_type` | `"Person"` (read by `crm.py`, not in `config.py`) | Type filter of the search request (MiniCRM's *Személy* filter): only person cards are returned. `""` = no filter; the person is then searched for by paging |
| `search_max_pages` | `8` (read by `crm.py`, not in `config.py`) | Upper limit of result pages fetched when paging is needed (1-20) |
| `company_lookup` | `true` (read by `crm.py`, not in `config.py`) | `false` switches stage 2 (the company request) off completely |
| `company_request` | none (read by `crm.py`, not in `config.py`) | Form data of the contact detail request, with `{id}` for the contact id: a JSON object (below) or the raw form text copied from DevTools. **Stage 2 is off while this is empty** |
| `company_path` | `""` (optional) | Exact place of the company in the answer, `>`-separated, `{id}` allowed, e.g. `Rpc/Account/MainInfo/{id} > Body > BusinessRelated > Name`. Empty = automatic search for the field names listed above |
| `company_same_name` | `true` (read by `crm.py`, not in `config.py`) | Only profiles with the same name as the shown caller may supply the company (duplicate profiles). `false` = any profile with the number |
| `warmup` | `true` (read by `crm.py`, not in `config.py`) | Keep one MiniCRM connection warm with a light request |
| `warmup_interval_s` | `30` (minimum `10`) | Seconds between warm-up requests |
| `timeout_s` | `4` | Timeout per MiniCRM request |
| `debug_dump` | `true` in code, `false` in shipped `config.json` | Saves raw MiniCRM responses to `%LOCALAPPDATA%\CallCompanion\last_lookup_<attempt>_p<page>.json`, e.g. `last_lookup_1of3_p1.json` or `last_lookup_deals_p1.json`, plus `last_lookup.json` = the most recent (**contains customer data**) |
| `browsers`, `cookie_domain` | `chrome/edge/firefox`, `minicrm.hu` | Only for `cookie_source = browser` |

#### Company name settings (stage 2)

Add this to the `minicrm` block of `config.json` (it is part of the shipped `config.json` from 0.3.1-beta):

```json
{
  "minicrm": {
    "mock": false,
    "debug_dump": false,
    "company_request": {
      "Requests[0][Method]": "GET",
      "Requests[0][Uri]": "Rpc/Account/MainInfo/{id}",
      "Requests[1][Method]": "GET",
      "Requests[1][Uri]": "Rpc/Account/DetailedInfo/{id}"
    }
  }
}
```

`company_path` is not needed: `BusinessRelated` is found automatically. To adapt the request to another MiniCRM
system: open a contact in MiniCRM with Chrome DevTools (F12) > Network open, find the `Batch` request, and copy its
*Form Data* (**view source**), replacing the contact id with `{id}`. Then watch `app.log` for the `company id=...` lines
(see [troubleshooting](#logs-and-troubleshooting)).

**`local_api`**: `enabled` (`true`), `host` (`127.0.0.1`), `port` (`48653`), `extension_id` (`""`, optional pin).

**`detectors.bria_log`**: `enabled` (`true`), `log_path` (`""`; optional exact log file or list of files, no search when
set), `log_globs` (see above), `log_globs_fallback` (see above), `incoming_regex` (`incoming from`),
`outgoing_regex` (`Transmitting to`): the markers Bria writes before each SIP message.

**`ui`**

| Key | Default | Meaning |
|---|---|---|
| `margin_px` | `16` | Distance of the cards from the screen edge |
| `overlay_position` | `"bottom_right"` | Corner of the usable screen area: `bottom_right`, `bottom_left`, `top_right`, `top_left` |
| `offset_x` | `-380` | Pixels added to the horizontal position; negative = left. The default clears Bria's incoming-call toast |
| `offset_y` | `0` | Pixels added to the vertical position; negative = up |
| `dock_to_bria` | `false` | `true` = while Bria's toast is on screen, the cards sit directly left of it (bottoms aligned; on top of it when there is no room); otherwise the corner + offsets apply |
| `bria_process_regex` | `"(?i)bria"` | Program file name of Bria, for `dock_to_bria` |
| `dock_gap_px` | `12` | Gap between Bria's toast and the docked cards |
| `answered_fade_ms` | `2500` | **No longer used** since 0.4.0 (an answered call keeps its card until the call ends); kept so existing `config.json` files stay valid |
| `missed_persistent` | `true` (read by `main.py`, not in `config.py`) | `false` = missed calls fade out like before |
| `missed_max` | `4` (read by `main.py`, not in `config.py`) | Missed-call cards kept at once; the oldest is closed beyond that |
| `missed_close_on_action` | `true` (read by `main.py`, not in `config.py`) | `true` = opening the contact or the Google search closes a missed-call card; `false` = only **✕** closes it |

Example, in `%LOCALAPPDATA%\CallCompanion\config.json` (kept by updates; restart the app after editing):

```json
{"ui": {"offset_x": -450}}
```

**`update`** (auto-update, `updater.py`)

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | `false` = never check or update |
| `repo` | `""` = `version.GITHUB_REPO` | GitHub `owner/name` whose Releases are checked |
| `include_prereleases` | `true` | Pre-releases (all `-beta` versions) count as updates |
| `check_interval_h` | `6` | Re-check while running; `0` = only after start |
| `startup_delay_s` | `15` | First check this long after start |
| `asset_regex` | `^CallCompanion-Setup-.+\\.exe$` | Which release asset is the installer |
| `require_checksum` | `true` | Refuse an installer without a SHA-256 to compare against |
| `github_token` | `""` | Only if the repository becomes private (read-only token) |
| `timeout_s` | `15` | Timeout of the GitHub requests |

**Where settings come from**: `config.py` defaults, then `config.json` next to the exe (shipped, replaced by updates),
then `%LOCALAPPDATA%\CallCompanion\config.json` (optional, personal, kept by updates).

**Command line**: `--version`, `--show`, `--simulate NUMBER`, `--probe-bria [SECONDS]`, `--check-update`,
`--install-autostart`, `--remove-autostart` (source checkouts only), `--after-install` (used by the installer).

---

## Logs and troubleshooting

Everything is logged to `%LOCALAPPDATA%\CallCompanion\app.log` (cookies and response contents are never logged).
The status window's **Naplómappa megnyitása** button opens that folder.

| You see | Meaning / action |
|---|---|
| `Tailing Bria log: <path> (utf-...)` | The Bria log was found and is being followed |
| `event: ringing +36... (bria-log)` | Detection works for an incoming call |
| `event: ringing +36... (bria-log, out)` | An outgoing call was detected (the number is the dialled one) |
| `event: ended +36... (bria-log, cancel)` | The caller hung up before you answered: a missed-call card appears |
| `event: ended ... (bria-log, elsewhere\|rejected\|failed\|bye)` | Answered on another phone / declined by you / failed / normal hang-up: no missed-call card |
| Outgoing calls show no card | The outgoing INVITE is not recognised: run `--probe-bria` during an outgoing test call (see limits) |
| Every call from a ring group leaves a missed-call card | The PBX does not send `Reason: ... completed elsewhere`: keep the CANCEL lines of `--probe-bria` |
| No `event:` lines during a call | Bria Log Level is not Verbose, or the log path/markers differ: run `--probe-bria` |
| Bria row "Nem található: ..." | The followed log file disappeared: check the Bria profile folder |
| Bria row "Nem található Bria naplófájl (log_globs?)" | The watcher runs but no log file was found under `%APPDATA%\CounterPath Corporation\Bria Enterprise\`: start Bria once (Log Level = Verbose), or set `detectors.bria_log.log_path` to the exact file |
| Bria row "A napló N perce nem változott" | Bria may not be running, or the line is simply quiet |
| `lookup ... no MiniCRM session yet` / card says so | The extension has not pushed cookies yet: open Chrome with MiniCRM logged in; check the extension badge |
| `extension push (startup\|cookies\|heartbeat\|retry\|retry-fast\|poke\|user\|installed): N cookies` | The extension delivered the session (the reason shows what triggered the push; `retry-fast` and `poke` = it was waiting for the app and delivered within seconds of the app starting) |
| `session state: False` / "MiniCRM login needed" card | The MiniCRM session expired: log in again in Chrome |
| `session state: None no cookies` | No extension push yet: Chrome closed or extension not loaded |
| `connection warm-up active` / `failing` | The keep-alive request to MiniCRM works / does not work (logged on state changes only) |
| `lookup ... (N ms, warm=True)` | The lookup reused the warm connection |
| `recheck: requested (extension=...)` then `recheck: done minicrm=... extension=...` | The re-check button was pressed and finished |
| `recheck: no result after 8 s` | The session check did not answer in time |
| `status window placed at X,Y (screen ...)` | Position chosen when the status window was opened |
| `attempt 1/3 term=... type=Person page=1 -> HTTP 200 ... items=1 types={'Person': 1}` | The Person search works and found a person. One `attempt` line per lookup is the normal case. `body keys=[...]` at the end of the line shows whether MiniCRM sends a page or total count |
| `attempt 1/3 ... items=0 types={}` on every spelling, then `attempt deals term=... type=- page=1` | No person has that number; the deal fallback is being tried |
| `type filter 'Person' not applied by MiniCRM (got {...})`, then `paging term=...: N page(s) (...), fetching pages 2-N in parallel` | MiniCRM returned non-Person items despite the filter (or `search_type` is wrong): the paging safety net is looking for the person. Keep the line so `search_type` can be corrected |
| `deal fallback: N non-person item(s), M with the caller's number, K distinct name(s) -> used\|ambiguous\|none` | Result of the deal-card fallback: `used` = the name was taken from the deal cards; `ambiguous` = several different names, shown as unknown; `none` with `M=0` although deals exist = the sub-line layout differs from the assumption (use `debug_dump`) |
| `lookup ... (0 ms, cached)` | The answer came from the cache (found 12 h, not found 5 min) |
| `person item fields=[...] company=from <Field>\|none persons=N` | The first hit's field names (never values). `company=from ...` = the search result itself carried a company (no stage 2 needed). `persons=2` = a duplicate profile exists for the number |
| `company id=50 -> found=True from BusinessRelated (N ms)` | Stage 2 found the company on that profile. `(0 ms, cached)` = from the cache |
| `company id=50 -> none (N ms) fields=[...]` | That profile has no company. The field paths (names only) show where MiniCRM puts data, for choosing `company_path` |
| `company: candidate 2 of 2 had it (id=51)` | The company came from an alternate profile of the same person (duplicate profiles) |
| `company: skipped N candidate(s) with a different name (company_same_name)` | Another profile with the number belongs to someone else and was not used. Set `company_same_name` to `false` to accept it |
| `company id=... fetch failed: network ... / HTTP ... / not JSON / MiniCRM session expired / bad company_request` | The detail request failed; the card keeps the name without a company and the next call retries. `bad company_request` = the JSON/form text in `config.json` is malformed |
| No `company ...` lines at all, but the number is known | Stage 2 is off: `company_request` is missing in `config.json` (or `company_lookup` is `false`) |
| `attempt ... items=N types={...}` but "Ismeretlen hívó" | Results exist but none is a person and the deal fallback found no single name (or the number is not in MiniCRM) |
| `HTTP 404 from .../Batch` | The request URL is wrong: check `system_id` / `batch_url` |
| `unexpected response, not JSON` | MiniCRM returned a page instead of data (login page?): check the session |
| `unrecognised ...` / `unexpected JSON, keys=[...]` | MiniCRM changed its response format: keep the log line and the dump |

---

## Security and privacy

- The local API listens on `127.0.0.1` only. Extension endpoints accept only a `chrome-extension://` origin
  (optionally pinned to your extension id) and a localhost `Host` header; `/event` refuses browser-initiated requests.
- The extension sends the MiniCRM session cookies to the local app only. It does not contact any
  other server.
- Besides the update check (below), the only outside traffic from the app is to `r3.minicrm.hu`: the lookup, the company detail request (stage 2, up to 3
  per call), the session check and the warm-up request
  (about every 30 s while a session exists, carrying your session cookies like any normal MiniCRM request). The Google
  button simply opens a search in your browser.
- Cookie values are never written to the log. The `attempt` lines contain only field names, counts and item types; the one `lookup ...` line per call contains the caller's number and the resolved name. The `person item ...` and `company ...` lines contain contact ids, field names and counts, **never names or company names**.
- The status window's call table keeps the last 50 calls (numbers and resolved names) in memory only; it is cleared when
  the app quits.
- `debug_dump` files (including the company detail answers `last_lookup_company_<id>.json`) contain real customer data: keep it `false` except while diagnosing, and delete the dumps afterwards.
- `bria_probe.txt` contains phone numbers and SIP data: redact before sharing.
- **Updates**: the app calls `api.github.com` (release list, no personal data, no token) and downloads the installer from
  GitHub's download servers. The installer runs only after its SHA-256 matches the one published in the release.
  Turn it off with `"update": {"enabled": false}`.

---

## Roadmap

Done in 0.3.0-beta: status window, connection warm-up, extension retry and heartbeat, status
window placement and re-check feedback. Done in 0.3.1-beta: Person-only search with paging and deal-subtext fallbacks
(callers with many deal cards), expiry for cached results, the name / company / number card layout and the company
name in a second stage, including duplicate `Person` profiles. Done in 0.4.0-beta: faster extension sync, persistent
missed-call cards, the in-call duration counter and outgoing-call cards. Done in 0.5.0-beta: one-click installer,
uninstaller and automatic updates from GitHub Releases.

Next:

1. **Live test** with Bria Enterprise and the Chrome extension on the office PC: detection, lookup timing with warm-up,
   the company line on real calls, and the stale-log warning on a quiet line.
2. **Lookup timing breakdown** in the log (connect / server / parse), if the live test shows the lookup is still slow.
3. **Installer and auto-update** (built in 0.5.0-beta): first real build, install and update cycle on the colleague's
   PC; then code-signing.
4. **Clean-up**: remove the legacy `minicrm.search_url` / `minicrm.parse.*` keys and the unused `beautifulsoup4` / `lxml`
   packages.

Candidates after that: keeping the last session
encrypted per user so the first call after boot already works, an automated test suite, handling several simultaneous
calls, acrylic blur, Chrome Web Store (unlisted) publication of the extension.

---

## Project layout

```
callcompanion/
  main.py  detectors.py  events.py  local_api.py  crm.py  overlay.py  status_window.py  probe.py  updater.py
  config.py  config.json  version.py  requirements.txt
  extension/            manifest.json  background.js  content.js
  assets/               app.ico
  build.ps1  callcompanion.spec  installer.iss
  README.md  CHANGELOG.md  HANDOFF.md  .gitignore
```

Runtime data lives in `%LOCALAPPDATA%\CallCompanion\` (`app.log`, `app.lock`, optional personal `config.json`,
`updates\`, `update-install.log`, optional dumps and probe output).

---

## Versioning and releases

[Semantic Versioning](https://semver.org) with a pre-release label. `0.x` means beta: features and configuration may
still change. `1.0.0` is reserved for the first release with a verified standalone build. The Chrome extension carries
the same number in `extension/manifest.json` (`version` `0.4.0`; Chrome does not accept a pre-release label in
`version`, and the manifest currently has no `version_name`). The extension changed in 0.4.0-beta (start-up sync), so
its number moved from `0.3.0` to `0.4.0`.

To cut a release: update `version.py`, `CHANGELOG.md` (and `extension/manifest.json` if the extension changed), commit,
push, then `.\build.ps1 -Publish`. It creates the tag `v<version>` on the current commit together with the GitHub
release, and installed copies update to it (see [Building the installer](#building-the-installer-and-publishing-updates)).

See [CHANGELOG.md](CHANGELOG.md) for the history.
