"""MiniCRM lookup using the logged-in browser session (no API keys, no separate login).

MiniCRM's web app is a single-page app: the page you see at  https://r3.minicrm.hu/<system id>/#Search?Query=...
is filled from a background request (found with Chrome DevTools):

    POST https://r3.minicrm.hu/<system id>/Batch             (form data, see _batch_body)
    -> {"Rpc/List": {"Status": 200, "Body": {"Items": [{"Id":..,"Type":"Person","Name":..,"Url":"#AddressBook/<id>"}]}}}

We send the same request with the browser's session cookies and take the first item of Type "Person".
Optional config.json keys (minicrm): "system_id" (default 60171), "batch_url" (full URL override).
"""
import html as htmllib
import logging
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote, urljoin, urlparse

import requests

import config

log = logging.getLogger(__name__)

DEFAULT_SYSTEM_ID = "60171"


@dataclass
class CallerInfo:
    found: bool = False
    name: str = ""
    company: str = ""
    profile_url: str = ""
    elapsed_ms: int = 0
    error: str = ""


def _ci(d, key):
    """Case-insensitive dict.get that also tolerates a non-dict."""
    if not isinstance(d, dict):
        return None
    if key in d:
        return d[key]
    low = key.lower()
    return next((v for k, v in d.items() if str(k).lower() == low), None)


def _clean(value) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", "", value))).strip()


class MiniCrmClient:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.http = requests.Session()  # keep-alive -> lower per-lookup latency
        self.http.headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
        )
        self._lock = threading.Lock()
        self._sig = None
        self.has_session = False
        self._cache: dict[str, CallerInfo] = {}

    # ---- session sources -----------------------------------------------------------------
    def set_extension_session(self, cookies: list, user_agent: str = "") -> bool:
        """Replace the cookie jar with the cookies pushed by the Chrome extension.
        Returns True when the cookie set changed since the last push."""
        sig = hash(tuple(sorted((c["name"], c["value"], c.get("domain", "")) for c in cookies)))
        with self._lock:
            changed = sig != self._sig
            if changed:
                self.http.cookies.clear()
                for c in cookies:
                    self.http.cookies.set(
                        c["name"], c["value"],
                        domain=c.get("domain") or None,
                        path=c.get("path") or "/",
                        secure=bool(c.get("secure")),
                    )
                self._sig = sig
            if user_agent:
                self.http.headers["User-Agent"] = user_agent
            self.has_session = bool(cookies)
        return changed

    def load_cookies(self) -> str:
        """cookie_source = manual | browser. Raises RuntimeError."""
        header = self.cfg["cookie_header"].strip()
        if self.cfg["cookie_source"] == "manual" or header:
            if not header:
                raise RuntimeError("cookie_header is empty")
            self.http.headers["Cookie"] = header
            self.has_session = True
            return "manual cookie header"
        try:
            import browser_cookie3  # optional dependency
        except ImportError as e:
            raise RuntimeError("browser_cookie3 is not installed (pip install browser-cookie3)") from e
        errors = []
        for name in self.cfg["browsers"]:
            try:
                jar = getattr(browser_cookie3, name)(domain_name=self.cfg["cookie_domain"])
                n = 0
                for c in jar:
                    self.http.cookies.set_cookie(c)
                    n += 1
                if n:
                    self.has_session = True
                    return f"{name} ({n} cookies)"
                errors.append(f"{name}: no MiniCRM cookies")
            except Exception as e:  # Chrome 127+ app-bound encryption lands here
                errors.append(f"{name}: {e}")
        raise RuntimeError("; ".join(errors))

    def _is_login_url(self, url: str) -> bool:
        return self.cfg["login_url_hint"].lower() in urlparse(url).path.lower()

    def session_ok(self):
        """True = logged in, False = expired/not logged in, None = could not determine."""
        if self.cfg["mock"]:
            return True
        if not self.has_session:
            return False
        try:
            r = self.http.get(self.cfg["session_check_url"], timeout=self.cfg["timeout_s"],
                              allow_redirects=False)
        except requests.RequestException as e:
            log.warning("session check failed: %s", e)
            return None
        if r.status_code in (401, 403):
            return False
        if 300 <= r.status_code < 400:
            return not self._is_login_url(r.headers.get("Location", ""))
        return r.status_code == 200

    # ---- lookup ----------------------------------------------------------------------------
    def lookup(self, number: str) -> CallerInfo:
        t0 = time.perf_counter()
        if number in self._cache:
            return self._cache[number]
        if self.cfg["mock"]:
            time.sleep(0.12)
            info = CallerInfo(True, "Teszt Elek", "Példa Kft.", self.cfg["base_url"])
        elif not self.has_session:
            info = CallerInfo(error="no MiniCRM session yet")
        else:
            info = self._real_lookup(number)
        info.elapsed_ms = int((time.perf_counter() - t0) * 1000)
        log.info("lookup %s -> found=%s name=%r (%d ms) %s", number, info.found, info.name,
                 info.elapsed_ms, info.error)
        if not info.error:
            self._cache[number] = info
        return info

    def _real_lookup(self, number: str) -> CallerInfo:
        digits = re.sub(r"\D", "", number)
        if len(digits) < 6:      # internal extension / anonymous caller: nothing to look up
            return CallerInfo(found=False)
        hu = digits.startswith("36")
        ctx = {
            "digits": digits,
            "tail9": digits[-9:],
            "local": ("06" + digits[2:]) if hu else digits,   # 06301234567
            "e164": "+" + digits,                             # +36301234567 (form-encoded as %2B...)
        }
        # Search order: Hungarian local format first (what MiniCRM's own search accepts), then the
        # international form, then whatever extra variants config.json lists (default: last 9 digits).
        order = (["{local}", "{e164}"] if hu else ["{e164}", "{digits}"]) + list(self.cfg["search_variants"])
        terms, seen = [], set()
        for tmpl in order:
            t = tmpl.format(**ctx)
            if t not in seen:
                seen.add(t)
                terms.append(t)

        url, headers = self._batch_url(), self._ajax_headers()
        saw_json, problem = False, ""
        for n, raw_term in enumerate(terms, 1):
            t0 = time.perf_counter()
            try:
                r = self.http.post(url, data=self._batch_body(raw_term), headers=headers,
                                   timeout=self.cfg["timeout_s"], allow_redirects=True)
            except requests.RequestException as e:
                log.info("attempt %d/%d term=%s POST %s -> network error %s (%d ms)", n, len(terms),
                         raw_term, url, e.__class__.__name__, (time.perf_counter() - t0) * 1000)
                return CallerInfo(error=f"network: {e.__class__.__name__}")
            # One diagnostic line per attempt (every response, whatever the status; no cookies, no names).
            dump = self._dump(n, r) if self.cfg["debug_dump"] else ""
            log.info("attempt %d/%d term=%s -> HTTP %s POST %s %s (%d ms)%s", n, len(terms), raw_term,
                     r.status_code, r.url, self._summarize(r), (time.perf_counter() - t0) * 1000,
                     f" dump={dump}" if dump else "")
            if self._is_login_url(r.url) or r.status_code in (401, 403):
                return CallerInfo(error="MiniCRM session expired")
            if r.status_code in (400, 404):
                problem = f"HTTP {r.status_code} from {url}"
                continue
            if r.status_code != 200:
                return CallerInfo(error=f"HTTP {r.status_code}")
            try:
                data = r.json()
            except ValueError:                   # HTML (login / error page) instead of JSON
                problem = f"unexpected response, not JSON ({r.headers.get('Content-Type', '?')})"
                continue
            saw_json = True
            info = self._parse_batch(data, raw_term)
            if info.found or info.error:
                return info
        if not saw_json and problem:             # never got a usable answer: show it, don't say "unknown"
            return CallerInfo(error=problem)
        return CallerInfo(found=False)

    # ---- MiniCRM BatchRequest ----------------------------------------------------------------
    def _system_id(self) -> str:
        return str(self.cfg.get("system_id") or DEFAULT_SYSTEM_ID)

    def _site(self) -> str:
        return f"{self.cfg['base_url'].rstrip('/')}/{self._system_id()}/"

    def _batch_url(self) -> str:
        return self.cfg.get("batch_url") or self._site() + "Batch"

    def _ajax_headers(self) -> dict:
        """Same headers the MiniCRM web app sends with its own background (XHR) requests."""
        u = urlparse(self.cfg["base_url"])
        return {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": f"{u.scheme}://{u.netloc}",
            "Referer": self._site(),
        }

    @staticmethod
    def _batch_body(term: str) -> dict:
        """Form data of the full-search request (as captured in Chrome DevTools). '+' is form-encoded by
        requests as %2B, so +36... arrives intact."""
        return {
            "Requests[0][Method]": "GET",
            "Requests[0][Uri]": "Rpc/List",
            "Requests[0][Params][Definition]": "Page.Search.Index",
            "Requests[0][Params][CategoryId]": "",
            "Requests[0][Params][Page]": "0",
            "Requests[0][Params][Query]": term,
            "Requests[0][Params][Type]": "",
            "Requests[0][Params][UserId]": "",
            "Controller": "Search_Index",
            "Uri": "Search",
        }

    def _parse_batch(self, data, term: str) -> CallerInfo:
        """{"Rpc/List": {"Status":200, "Body": {"Items":[...]}}} -> first item of Type "Person".
        No such item = not found. A reply we cannot read is an error (field names only), never 'unknown'."""
        node = _ci(data, "Rpc/List")
        if not isinstance(node, dict):
            keys = sorted(data)[:6] if isinstance(data, dict) else type(data).__name__
            return CallerInfo(error=f"unexpected JSON, keys={keys}")
        status = _ci(node, "Status")
        if status in (401, 403):
            return CallerInfo(error="MiniCRM session expired")
        if status not in (None, 200):
            return CallerInfo(error=f"Rpc/List status {status}")
        items = _ci(_ci(node, "Body"), "Items")
        if not isinstance(items, list):
            return CallerInfo(error="unexpected JSON: no Rpc/List > Body > Items list")
        for item in items:
            if str(_ci(item, "Type") or "").lower() != "person":
                continue
            name = _clean(_ci(item, "Name"))
            if not name:
                continue
            link = _ci(item, "Url")
            # Url is a browser route like "#AddressBook/21157"; without it open the search page instead
            profile = (urljoin(self._site(), link) if isinstance(link, str) and link.strip()
                       else self._site() + "#Search?Query=" + quote(term, safe=""))
            # The search result carries no company name (Content = e-mail + phone), so it stays empty.
            return CallerInfo(True, name, "", profile)
        return CallerInfo(found=False)

    @staticmethod
    def _summarize(r) -> str:
        """Short, value-free description of a response: type, size, redirects, <title> or JSON shape."""
        ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
        parts = [f"type={ctype or '?'}", f"{len(r.content)}B"]
        if r.history:
            parts.append("redirects=" + ">".join(str(h.status_code) for h in r.history))
        try:
            if "json" in ctype:
                data = r.json()
                if isinstance(data, dict):
                    parts.append(f"json keys={sorted(data)[:8]}")
                    node = _ci(data, "Rpc/List")
                    if isinstance(node, dict):
                        parts.append(f"status={_ci(node, 'Status')}")
                        items = _ci(_ci(node, "Body"), "Items")
                        if isinstance(items, list):      # item TYPES only, never names/contact data
                            types = [str(_ci(i, "Type")) for i in items[:6] if isinstance(i, dict)]
                            parts.append(f"items={len(items)} types={types}")
                elif isinstance(data, list):
                    parts.append(f"json list len={len(data)}")
                else:
                    parts.append("json scalar")
            elif r.content:
                m = re.search(r"(?is)<title[^>]*>(.*?)</title>", r.text)
                parts.append("title=%r" % (re.sub(r"\s+", " ", m.group(1)).strip()[:80] if m else None))
        except Exception:
            pass
        return " ".join(parts)

    @staticmethod
    def _dump(n: int, r) -> str:
        """debug_dump: save each attempt's raw response next to app.log (contains customer data!).
        last_lookup_<n>.<ext> per attempt, plus last_lookup.<ext> = the most recent one."""
        ext = ".json" if "json" in r.headers.get("Content-Type", "").lower() else ".html"
        try:
            d = config.data_dir()
            (d / f"last_lookup_{n}{ext}").write_text(r.text, encoding="utf-8")
            (d / f"last_lookup{ext}").write_text(r.text, encoding="utf-8")
            return f"last_lookup_{n}{ext}"
        except OSError as e:
            log.warning("debug dump failed: %s", e)
            return ""
