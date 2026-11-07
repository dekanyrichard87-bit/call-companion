# Call Companion

**Version 0.2.0-beta** · Windows · Python 3.11+ / PySide6

Call Companion is a small tray application that watches the **Bria Enterprise** softphone and, the moment a
call rings, shows a dark, frameless pop-up card in the bottom-right corner of the screen. The card shows the
caller's number immediately and, a moment later, the caller's **name from MiniCRM**, with one-click buttons to
open the contact in MiniCRM, search the number on Google, or copy it.

It needs **no API keys and no separate MiniCRM login**: it reuses the MiniCRM session of the Chrome profile you
are already logged in with, delivered by a tiny Chrome extension.

- [How it works](#how-it-works)
- [What works in this release](#what-works-in-this-release)
- [Known limits and workarounds](#known-limits-and-workarounds)
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
   cookies of *.minicrm.hu                                              |
        |  POST 127.0.0.1:48653/ext/session                              |  tailed every 0.1 s
        v                                                                v
   local_api.LocalApi                                       detectors.BriaLogDetector
        |  session cookies                                      |  CallEvent: ringing / answered / ended
        v                                                       v
   crm.MiniCrmClient  <---- lookup(number) ----  main.Controller (Qt main thread)
        |                                                       |
        |  POST https://r3.minicrm.hu/60171/Batch               |  show / update / fade out
        v                                                       v
   MiniCRM  --> name + profile link  ---------------->  overlay.CallOverlay (pop-up card)
```

**Life of an incoming call**

1. Bria logs the SIP `INVITE`. `BriaLogDetector` reads the caller from the `From:` header, normalises it
   (`00036301234567` -> `+36301234567`) and emits a `ringing` event.
2. The controller shows the card **at once** with the number and "Looking up...".
3. A background thread asks MiniCRM for that number (see [lookup](#minicrm-lookup)). The card updates with the
   name and enables the MiniCRM button. Repeat callers are answered from an in-memory cache.
4. When you answer, the card shrinks to a compact "IN CALL" view and fades out after 2.5 s.
5. When the call ends or is missed (`BYE`, `CANCEL`, or a 3xx-6xx reply before answering), the card fades out and
   is destroyed.

**Components**

| File | Role |
|---|---|
| `main.py` | Entry point, tray icon and menu, `Controller` (glues events, lookup and card together), autostart |
| `detectors.py` | `BriaLogDetector`: tails Bria's `Log.txt` and rebuilds call state from the SIP messages in it |
| `events.py` | `CallEvent` and `normalize_number` |
| `local_api.py` | Local HTTP server on `127.0.0.1:48653` for the extension (`/ext/session`, `/ext/ping`) and test events (`/event`) |
| `crm.py` | `MiniCrmClient`: session cookies, MiniCRM lookup, caching, diagnostics |
| `overlay.py` | `CallOverlay`: the frameless dark card, states, buttons, fade animations |
| `extension/` | Chrome MV3 extension that relays the MiniCRM session cookies to the local API |
| `probe.py` | `--probe-bria`: captures what Bria writes during a test call (for adapting the parser) |
| `config.py` / `config.json` | Defaults and your overrides (deep-merged) |
| `version.py` | Version number (single source of truth) |
| `build.ps1`, `callcompanion.spec`, `install.ps1`, `uninstall.ps1` | Packaging, install and autostart scripts |

### MiniCRM lookup

MiniCRM's web interface is a single-page app, so there is no page to download and parse. The app sends the same
background request the MiniCRM search page sends:

```
POST https://r3.minicrm.hu/60171/Batch          (form data: Rpc/List, Page.Search.Index, Query=<number>)
-> {"Rpc/List": {"Status": 200, "Body": {"Items": [{"Type": "Person", "Name": "...", "Url": "#AddressBook/<id>"}]}}}
```

It takes the **first item of Type `Person`**: its `Name` is shown and its `Url` becomes the MiniCRM button.
The number is searched in this order, stopping at the first hit:

| Hungarian number | Other numbers |
|---|---|
| 1. `06301234567` | 1. `+4915123456789` |
| 2. `+36301234567` (sent as `%2B...`) | 2. `4915123456789` |
| 3. last 9 digits (`301234567`) | 3. last 9 digits |

Numbers shorter than 6 digits (internal extensions, "Anonymous") are not looked up. Failures are shown on the card
("Lookup failed" plus the reason) instead of a misleading "Unknown caller".

---

## What works in this release

- **Caller resolution from MiniCRM** through the `Batch` search request, with the three-step number fallback above
  and an in-memory cache.
- **Pop-up card** with live updates: ringing, answered (compact view, auto fade), ended or missed (immediate fade),
  and a notice card when the MiniCRM login has expired. Buttons: MiniCRM profile, Google search, copy number, close.
  The card sits above the taskbar, never steals keyboard focus, and survives back-to-back calls.
- **Direct profile opening**: the MiniCRM button opens the contact (`.../60171/#AddressBook/<id>`) in the default browser.
- **Session sync without any login**: the Chrome extension pushes the MiniCRM cookies at browser start, on every
  cookie change, and once a minute. The app warns when the session is missing or expired.
- **Bria Enterprise detection** from `Logs\Log.txt` (ring, answer, hang-up, missed call, rejected call; outgoing
  calls are ignored; mid-call re-INVITEs do not close the card).
- **Windows integration**: tray icon, single-instance lock, per-user autostart (no admin rights).
- **Diagnostics**: one log line per MiniCRM attempt (status, URL, item types, timing), optional raw response dumps,
  and `--probe-bria` to capture Bria's log format.

---

## Known limits and workarounds

| Limit | Why | Workaround / plan |
|---|---|---|
| **First call after (re)start can fail** with "no MiniCRM session yet" | The app starts before the extension has delivered the cookies; the next push waits for the once-a-minute timer | Open Chrome (with MiniCRM logged in) and wait up to a minute after login. Fix planned: [extension retry](#roadmap) |
| **Lookup takes about 1.2 s** (measured on the first lookup after a session sync) | Includes opening the HTTPS connection and MiniCRM's full-search processing; not yet broken down. The number is shown instantly, only the name arrives later | Repeat callers are cached and instant. Planned: connection warm-up and a timing breakdown |
| **No company name** | MiniCRM's search result contains only name, e-mail and phone for a person | Planned: optional second request for the contact record |
| **Callers not found are cached until the app restarts** | The cache stores "not found" too | If you add a caller to MiniCRM, quit and restart the app from the tray menu. Planned: cache expiry |
| Only the **first matching person** is shown | Deliberate for now | - |
| One call card at a time | A new ringing call replaces the previous card | - |
| Needs Bria **Log Level = Verbose** | At lower levels Bria writes no SIP messages | Help > Troubleshooting > Support. Verbose logs grow quickly; log rotation behaviour has not been tested |
| Chrome developer-mode extension | The extension is loaded unpacked, so Chrome shows a developer-mode warning at some start-ups | Planned: publish it unlisted on the Chrome Web Store |
| Hungarian-centric number handling | `+36`, `06`, `0036` and PBX-prefixed `000 36...` forms are normalised; other countries are searched as `+<digits>` | - |
| "Glass" effect is translucent only | No acrylic blur yet | - |
| **Packaging not verified** | `build.ps1` / `install.ps1` exist but have not yet been run on a clean Windows machine; the `.exe` is unsigned, so SmartScreen may warn | See roadmap |
| No automated tests in the repository | The behaviour was verified with throw-away scripts and live calls | Planned |

Unused configuration keys `minicrm.search_url` and `minicrm.parse.*` are leftovers from an earlier HTML-parsing
approach and are ignored. They will be removed in a later release.

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

A blue dot appears in the system tray. Hover it for the MiniCRM session status. Right-click: **Test incoming
call**, **Re-check MiniCRM session**, **Quit**. Check the version with `python main.py --version`.

To try only the pop-up (no Bria, no MiniCRM) set `"mock": true` in `config.json` and run
`python main.py --simulate 06301234567`.

### 2. Bria Enterprise

1. In Bria: **Help > Troubleshooting > Support > Log Level = Verbose**, then restart Bria.
2. The default log path is `%APPDATA%\CounterPath Corporation\Bria Enterprise\*\*\Logs\Log.txt`; the newest file
   wins. Change `detectors.bria_log.log_globs` if yours differs.
3. Check it: `python main.py --probe-bria 90`, then ring, answer and hang up within 90 s. The last line must say
   `Parser recognised 3 event(s): ['ringing','answered','ended']`. If not, keep `%LOCALAPPDATA%\CallCompanion\bria_probe.txt`
   (it contains phone numbers; redact before sharing) so the parser can be adapted.

### 3. Chrome extension (pairing)

1. Open `chrome://extensions`, switch on **Developer mode**, click **Load unpacked**, choose the `extension` folder.
2. Log in to MiniCRM in **that Chrome profile**.
3. With the app running, `app.log` shows `extension push (...): N cookies` and the tray tooltip changes to
   *MiniCRM: logged in*. A red `!` on the extension icon means the app is not reachable.
4. Optional hardening: copy the extension's ID from `chrome://extensions` into `local_api.extension_id` so only
   that extension may talk to the app.

The extension needs only the `cookies` and `alarms` permissions for `*.minicrm.hu` and talks only to
`127.0.0.1:48653` (the port must match `config.json` and `extension/background.js`).

### 4. Test without a real call

```powershell
Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:48653/event?type=ring&number=%2B36301234567&id=t1"
Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:48653/event?type=answer&id=t1"
Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:48653/event?type=end&id=t1"
```

### 5. Standalone build and autostart (not yet verified on a clean machine)

```powershell
.\build.ps1                    # -> dist\CallCompanion\ (exe, config.json, extension\) and dist\install.ps1
cd dist
.\install.ps1                  # copies to %LOCALAPPDATA%\Programs\CallCompanion, enables autostart, starts the app
```

`install.ps1` keeps your edited `config.json` when updating; `uninstall.ps1` removes the app and autostart.
Autostart alone: `python main.py --install-autostart` / `--remove-autostart` (per-user `Run` registry key).

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
| `timeout_s` | `4` | Timeout per MiniCRM request |
| `debug_dump` | `true` in code, `false` in shipped `config.json` | Saves raw MiniCRM responses to `%LOCALAPPDATA%\CallCompanion\last_lookup_<n>.*` (**contains customer data**) |
| `browsers`, `cookie_domain` | `chrome/edge/firefox`, `minicrm.hu` | Only for `cookie_source = browser` |

**`local_api`**: `enabled` (`true`), `host` (`127.0.0.1`), `port` (`48653`), `extension_id` (`""`, optional pin).

**`detectors.bria_log`**: `enabled` (`true`), `log_globs` (see above), `incoming_regex` (`incoming from`),
`outgoing_regex` (`Transmitting to`): the markers Bria writes before each SIP message.

**`ui`**: `answered_fade_ms` (`2500`), `margin_px` (`16`).

**Command line**: `--version`, `--simulate NUMBER`, `--probe-bria [SECONDS]`, `--install-autostart`, `--remove-autostart`.

---

## Logs and troubleshooting

Everything is logged to `%LOCALAPPDATA%\CallCompanion\app.log` (cookies and response contents are never logged).

| You see | Meaning / action |
|---|---|
| `event: ringing +36... (bria-log)` | Detection works |
| No `event:` lines during a call | Bria Log Level is not Verbose, or the log path/markers differ: run `--probe-bria` |
| `lookup ... no MiniCRM session yet` / card says so | The extension has not pushed cookies yet: open Chrome with MiniCRM logged in; check the extension badge |
| `session state: False` / "MiniCRM login needed" card | The MiniCRM session expired: log in again in Chrome |
| `attempt 1/3 ... HTTP 200 ... items=1 types=['Person']` | The lookup works and found a person |
| `attempt ... items=N types=[...]` but "Unknown caller" | Results exist but none is a `Person` (or the number is not in MiniCRM) |
| `HTTP 404 from .../Batch` | The request URL is wrong: check `system_id` / `batch_url` |
| `unexpected response, not JSON` | MiniCRM returned a page instead of data (login page?): check the session |
| `unrecognised ...` / `unexpected JSON, keys=[...]` | MiniCRM changed its response format: keep the log line and the dump |

---

## Security and privacy

- The local API listens on `127.0.0.1` only. Extension endpoints accept only a `chrome-extension://` origin
  (optionally pinned to your extension id) and a localhost `Host` header; `/event` refuses browser-initiated requests.
- The only outside traffic is to `r3.minicrm.hu` (the lookup). The Google button simply opens a search in your browser.
- Cookie values are never written to the log. Only field names, counts and item types are logged, not names or contact data.
- `debug_dump` files contain real customer data: keep it `false` except while diagnosing, and delete the dumps afterwards.
- `bria_probe.txt` contains phone numbers and SIP data: redact before sharing.

---

## Roadmap

Planned for the next milestones:

1. **System tray status window**: double-click the tray icon for a window with Bria connection, MiniCRM session,
   extension status, last calls and the log location.
2. **Connection warm-up**: open and keep the MiniCRM connection alive so the first lookup is faster, plus a timing
   breakdown in the log to see where the time goes.
3. **Extension retry logic**: retry every 30 s while the app is not reachable, so the session arrives within seconds
   of login (optionally keep the last session encrypted per user so the first call after boot already works).
4. **Standalone `.exe`**: verify the PyInstaller build and install scripts on a clean Windows machine, then code-sign.

Candidates after that: company name for the card, expiry for "not found" cache entries, removing the legacy
config keys, an automated test suite, handling several simultaneous calls, acrylic blur, Chrome Web Store
(unlisted) publication of the extension.

---

## Project layout

```
callcompanion/
  main.py  detectors.py  events.py  local_api.py  crm.py  overlay.py  probe.py
  config.py  config.json  version.py  requirements.txt
  extension/            manifest.json  background.js
  build.ps1  callcompanion.spec  install.ps1  uninstall.ps1
  README.md  CHANGELOG.md  .gitignore
```

Runtime data lives in `%LOCALAPPDATA%\CallCompanion\` (`app.log`, `app.lock`, optional dumps and probe output).

---

## Versioning and releases

[Semantic Versioning](https://semver.org) with a pre-release label. `0.x` means beta: features and configuration may
still change. `1.0.0` is reserved for the first release with a verified standalone build. The Chrome extension carries the same
number in `extension/manifest.json` (`version` `0.2.0`, `version_name` `0.2.0-beta`; Chrome does not accept a
pre-release label in `version`).

To cut a release: update `version.py`, `extension/manifest.json` and `CHANGELOG.md`, commit, then

```powershell
git tag -a v0.2.0-beta -m "Call Companion 0.2.0-beta"
git push origin v0.2.0-beta      # if the repository has a remote
```

See [CHANGELOG.md](CHANGELOG.md) for the history.
