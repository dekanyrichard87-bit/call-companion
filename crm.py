"""MiniCRM lookup using the logged-in browser session (no API keys, no separate login).

MiniCRM's web app is a single-page app: the page you see at  https://r3.minicrm.hu/<system id>/#Search?Query=...
is filled from a background request (found with Chrome DevTools):

    POST https://r3.minicrm.hu/<system id>/Batch             (form data, see _batch_body)
    -> {"Rpc/List": {"Status": 200, "Body": {"Items": [{"Id":..,"Type":"Person","Name":..,"Url":"#AddressBook/<id>"}]}}}

How a caller is resolved (cheapest first):

 1. Person-only search. Requests[0][Params][Type] = "Person" (what the search page's "Személy" filter sends)
    makes MiniCRM return only Person cards. A number that is tied to dozens of deals ("Adatlap") therefore
    still answers on page 1. The first Person with a name wins.
 2. Paging. If the server ever ignores the filter (non-Person items come back) or the filter is switched off
    ("search_type": ""), the remaining result pages are fetched in parallel until a Person turns up.
 3. Deal subtext. If no Person exists at all, the contact name is read from the sub-line of the deal cards
    ("Kis István, 06305256058"), but only when every matching card names the same person. Ambiguous = unknown.

Company name: the search result of a Person normally carries none (Content = e-mail + phone). If the Person item
has a recognisable company field (see _COMPANY_KEYS) it is passed on as CallerInfo.company; the log line
"person item fields=[...]" lists the field NAMES of the item, so the real field can be identified (no values).

Results are cached with an expiry: found 12 h, not found 5 min, errors never.

Duplicate profiles: one number can belong to several Person items (e.g. a private and a business profile of the
same caller). The name is taken from the first hit at once; stage 2 then tries the candidates in search order
(at most COMPANY_MAX_CANDIDATES) and uses the first one that has a company. By default ("company_same_name") only
candidates with the SAME name count, so a colleague or relative who shares the number never lends their company.

Stage 2, the company name (fetch_company): the name lookup above is never slowed down. The controller shows the
name first and then asks fetch_company() in the background, which sends ONE more Batch request for the person's
detail (the "#AddressBook/<id>" contact) and takes the FIRST company out of the answer. What that request looks like
is MiniCRM specific and comes from config.json ("company_request"); without it stage 2 stays off.

Optional config.json keys (minicrm): "system_id" (default 60171), "batch_url" (full URL override),
"search_type" (default "Person", "" = no type filter), "search_max_pages" (default 8, paging cap),
"company_lookup" (default true), "company_same_name" (default true), "company_request" (form data of the detail request, a dict or the raw
"a=b&c=d" text from DevTools, with {id} where the contact id goes; default none = stage 2 off),
"company_path" (optional, where the company is in the answer, {id} = contact id, e.g.
"Rpc/Account/MainInfo/{id} > Body > BusinessRelated > Name"; default: search the answer for the first field
called BusinessRelated / Company / Business / ...).
"""
import html as htmllib
import logging
import math
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from collections import deque
from dataclasses import dataclass, field, replace
from urllib.parse import parse_qsl, quote, urljoin, urlparse

import requests

import config

log = logging.getLogger(__name__)

DEFAULT_SYSTEM_ID = "60171"
DEFAULT_SEARCH_TYPE = "Person"
DEFAULT_MAX_PAGES = 8
PAGE_GUESS = 20            # a page with at least this many items is assumed to be a full one (MiniCRM: 25)

# item fields that never hold the "name, phone" sub-line of a deal card
_SKIP_FIELDS = {"id", "type", "name", "url", "icon", "image", "color", "content"}
_SEGMENT = re.compile(r"[,;\n]")

_COMPANY_ID = re.compile(r"#AddressBook/(\d+)")        # the contact route of a person ("#AddressBook/21157")

# Field names a company can be stored under. "BusinessRelated" is CONFIRMED (real MainInfo answer: Body >
# BusinessRelated > Name); the rest are guesses for other answers. First match wins, so the real one is first.
_COMPANY_KEYS = ("BusinessRelated", "CompanyName", "Company", "BusinessName", "Business", "Organization",
                 "Organisation", "Employer")


@dataclass
class CallerInfo:
    found: bool = False
    name: str = ""
    company: str = ""
    profile_url: str = ""
    elapsed_ms: int = 0
    error: str = ""
    alternates: list = field(default_factory=list)   # other Person hits for the same number: (name, profile_url)

    @property
    def company_name(self) -> str:
        """Alias of `company` (the field the call card reads)."""
        return self.company


@dataclass
class _Page:
    """One Batch response: either readable items, or a terminal error (stop), or a soft problem."""
    items: list | None = None
    data: object = None
    stop: CallerInfo | None = None      # lookup must end with this error (network, expired session, ...)
    problem: str = ""                   # this attempt is unusable (404, not JSON): try the next term


@dataclass
class _Outcome:
    person: CallerInfo | None = None
    items: list = field(default_factory=list)
    stop: CallerInfo | None = None
    problem: str = ""
    answered: bool = False              # at least one readable JSON answer


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


def _company_value(value) -> str:
    """Company name out of a field value: text, an object with a Name, or a list of those (the FIRST one wins)."""
    if isinstance(value, list):
        for v in value:
            company = _company_value(v)
            if company:
                return company
        return ""
    if isinstance(value, dict):
        value = _ci(value, "Name")
    return _clean(value)


def _is_person(item) -> bool:
    return str(_ci(item, "Type") or "").lower() == "person"


def _plausible_name(s: str) -> bool:
    """A person's name, not an e-mail, phone number or empty string."""
    return 2 <= len(s) <= 60 and any(ch.isalpha() for ch in s) \
        and not any(ch.isdigit() for ch in s) and "@" not in s


class MiniCrmClient:
    FOUND_TTL_S = 12 * 3600     # a resolved name rarely changes
    COMPANY_TTL_S = 12 * 3600   # same for a company; "no company" is re-checked after NOT_FOUND_TTL_S
    COMPANY_MAX_CANDIDATES = 3  # Person profiles tried per call in stage 2 (one detail request each)
    NOT_FOUND_TTL_S = 300       # "unknown" is re-checked after 5 min, so a freshly added contact is picked up

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
        self._cache: dict[str, tuple[float, CallerInfo]] = {}   # number -> (expires at, result)
        self._company_cache: dict[str, tuple[float, str]] = {}  # contact id -> (expires at, company or "")
        self._warm_stop = threading.Event()
        self._warm_kick = threading.Event()
        self._warm_thread = None
        self._warm_ok_at = 0.0          # time.monotonic() of the last successful warm-up ping

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

    # ---- connection warm-up ----------------------------------------------------------------
    # A request on a cold connection pays DNS + TCP + TLS (typically 300-500 ms); on an already open
    # keep-alive connection only the server's own time. A cheap HEAD every ~30 s keeps one connection
    # in the Session pool warm, so the lookup at the moment of a call reuses it.
    # Optional config.json keys (minicrm): "warmup" (default true), "warmup_interval_s" (default 30).
    def _warm_interval(self) -> float:
        try:
            return max(10.0, float(self.cfg.get("warmup_interval_s", 30)))
        except (TypeError, ValueError):
            return 30.0

    def is_warm(self) -> bool:
        return time.monotonic() - self._warm_ok_at < 2 * self._warm_interval()

    def start_warmup(self):
        if self.cfg["mock"] or not self.cfg.get("warmup", True):
            return
        if self._warm_thread is not None and self._warm_thread.is_alive():
            return
        self._warm_stop.clear()
        self._warm_thread = threading.Thread(target=self._warm_loop, daemon=True, name="crm-warmup")
        self._warm_thread.start()

    def kick_warmup(self):
        """Warm up right now (e.g. the session just became valid)."""
        self._warm_kick.set()

    def stop_warmup(self):
        self._warm_stop.set()
        self._warm_kick.set()

    def _warm_once(self) -> bool:
        t0 = time.perf_counter()
        try:
            # Any answer (even a redirect or 405) means the connection is open and reusable.
            self.http.head(self._site(), timeout=min(5, self.cfg["timeout_s"]), allow_redirects=False)
        except requests.RequestException as e:
            log.info("warm-up failed: %s", e.__class__.__name__)
            return False
        self._warm_ok_at = time.monotonic()
        log.debug("warm-up ok (%d ms)", (time.perf_counter() - t0) * 1000)
        return True

    def _warm_loop(self):
        interval, was_ok = self._warm_interval(), None
        while not self._warm_stop.is_set():
            if self.has_session:
                ok = self._warm_once()
                if ok != was_ok:         # log state changes only, not every ping
                    log.info("connection warm-up %s", "active" if ok else "failing")
                    was_ok = ok
            self._warm_kick.wait(interval)
            self._warm_kick.clear()

    # ---- cache -----------------------------------------------------------------------------
    def _cache_get(self, number: str) -> CallerInfo | None:
        entry = self._cache.get(number)
        if entry is None:
            return None
        expires, info = entry
        if time.monotonic() >= expires:
            self._cache.pop(number, None)
            return None
        return info

    def _cache_put(self, number: str, info: CallerInfo) -> None:
        ttl = self.FOUND_TTL_S if info.found else self.NOT_FOUND_TTL_S
        self._cache[number] = (time.monotonic() + ttl, info)

    # ---- lookup ----------------------------------------------------------------------------
    def lookup(self, number: str) -> CallerInfo:
        t0 = time.perf_counter()
        cached = self._cache_get(number)
        if cached is not None:
            info = replace(cached, elapsed_ms=int((time.perf_counter() - t0) * 1000))
            log.info("lookup %s -> found=%s name=%r (%d ms, cached)", number, info.found, info.name,
                     info.elapsed_ms)
            return info
        if self.cfg["mock"]:
            time.sleep(0.12)
            info = CallerInfo(True, "Teszt Elek", "Példa Kft.", self.cfg["base_url"])
        elif not self.has_session:
            info = CallerInfo(error="no MiniCRM session yet")
        else:
            info = self._real_lookup(number)
        info.elapsed_ms = int((time.perf_counter() - t0) * 1000)
        log.info("lookup %s -> found=%s name=%r (%d ms, warm=%s) %s", number, info.found, info.name,
                 info.elapsed_ms, self.is_warm(), info.error)
        if not info.error:                       # errors (network, expired session, ...) are never cached
            self._cache_put(number, info)
        return info

    def _search_type(self) -> str:
        value = self.cfg.get("search_type", DEFAULT_SEARCH_TYPE)
        return value.strip() if isinstance(value, str) else DEFAULT_SEARCH_TYPE

    def _max_pages(self) -> int:
        try:
            return max(1, min(20, int(self.cfg.get("search_max_pages", DEFAULT_MAX_PAGES))))
        except (TypeError, ValueError):
            return DEFAULT_MAX_PAGES

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

        search_type = self._search_type()
        problem, answered = "", False
        deal_items, deal_term = [], terms[0]
        # Pass 1: Person search (page 1 is enough while the type filter works; paging only as a safety net).
        for n, term in enumerate(terms, 1):
            out = self._collect(term, search_type, f"{n}/{len(terms)}", paging=True)
            if out.stop:
                return out.stop
            if out.problem:
                problem = out.problem
                continue
            answered = True
            if out.person:
                return out.person
            if out.items and not deal_items:     # the filter was ignored: these are all the items we saw
                deal_items, deal_term = out.items, term
        # Pass 2: no Person under any spelling. One unfiltered look at the first spelling: the number may
        # still be known as the contact of deals ("Adatlap"). Costs one extra request for unknown callers.
        if search_type and answered and not deal_items:
            out = self._collect(terms[0], "", "deals", paging=False)
            if out.stop:
                return out.stop
            if out.person:
                return out.person
            deal_items = out.items
        if deal_items:
            info = self._name_from_deals(deal_items, digits, deal_term)
            if info:
                return info
        if not answered and problem:             # never got a usable answer: show it, don't say "unknown"
            return CallerInfo(error=problem)
        return CallerInfo(found=False)

    # ---- one search: page 1, plus the remaining pages when they can hide the Person --------------
    def _collect(self, term: str, search_type: str, tag: str, paging: bool) -> _Outcome:
        out = _Outcome()
        first = self._fetch(term, 0, search_type, tag)
        if first.stop:
            out.stop = first.stop
            return out
        if first.problem:
            out.problem = first.problem
            return out
        out.answered = True
        out.items = list(first.items)
        out.person = self._person_from_items(first.items, term)
        if out.person or not first.items:
            return out
        others = [i for i in first.items if not _is_person(i)]
        filter_ignored = bool(search_type) and bool(others)
        if filter_ignored:
            log.warning("type filter %r not applied by MiniCRM (got %s): falling back to paging",
                        search_type, self._type_counts(first.items))
        if not paging or (search_type and not filter_ignored):
            return out
        for page in self._fan_out(term, search_type, tag, first):
            if page.stop or page.problem:        # cannot rule the Person out: report, do not say "unknown"
                out.stop = page.stop or CallerInfo(error=page.problem)
                return out
            out.items.extend(page.items)
            person = self._person_from_items(page.items, term)
            if person:
                out.person = person
                return out
        return out

    def _fan_out(self, term: str, search_type: str, tag: str, first: _Page) -> list:
        pages, how = self._page_count(first.data, len(first.items))
        if pages <= 1:
            return []
        log.info("paging term=%s: %d page(s) (%s), fetching pages 2-%d in parallel", term, pages, how, pages)
        wanted = list(range(1, pages))
        with ThreadPoolExecutor(max_workers=min(6, len(wanted)), thread_name_prefix="crm-page") as ex:
            return list(ex.map(lambda p: self._fetch(term, p, search_type, tag), wanted))

    def _page_count(self, data, n_items: int):
        """(number of result pages, where that came from). Uses a count field of the response when there
        is one; otherwise a full-looking page is assumed to have followers. Capped by search_max_pages."""
        cap = self._max_pages()
        body = _ci(_ci(data, "Rpc/List"), "Body")

        def num(key):
            v = _ci(body, key)
            return v if isinstance(v, int) and not isinstance(v, bool) else None

        for key in ("PageCount", "Pages", "TotalPages"):
            v = num(key)
            if v and v >= 1:
                return min(v, cap), f"Body.{key}"
        for key in ("TotalCount", "Total", "Count", "ItemCount", "TotalItems", "ResultCount", "AllCount"):
            v = num(key)
            if v is None or v < n_items:
                continue
            if v > n_items:
                return min(math.ceil(v / n_items), cap), f"Body.{key}={v}"
            if n_items < PAGE_GUESS:
                return 1, f"Body.{key}={v}"
        if n_items >= PAGE_GUESS:
            return cap, "assumed: no count in the response"
        return 1, "single page"

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
    def _batch_body(term: str, page: int = 0, search_type: str = "") -> dict:
        """Form data of the full-search request (as captured in Chrome DevTools). '+' is form-encoded by
        requests as %2B, so +36... arrives intact. Page is 0-based; Type "Person" = the Személy filter."""
        return {
            "Requests[0][Method]": "GET",
            "Requests[0][Uri]": "Rpc/List",
            "Requests[0][Params][Definition]": "Page.Search.Index",
            "Requests[0][Params][CategoryId]": "",
            "Requests[0][Params][Page]": str(page),
            "Requests[0][Params][Query]": term,
            "Requests[0][Params][Type]": search_type,
            "Requests[0][Params][UserId]": "",
            "Controller": "Search_Index",
            "Uri": "Search",
        }

    def _fetch(self, term: str, page: int, search_type: str, tag: str) -> _Page:
        """One Batch POST + one diagnostic line (every response, whatever the status; no cookies, no names)."""
        url, t0 = self._batch_url(), time.perf_counter()
        shown_type = search_type or "-"
        try:
            r = self.http.post(url, data=self._batch_body(term, page, search_type),
                               headers=self._ajax_headers(), timeout=self.cfg["timeout_s"],
                               allow_redirects=True)
        except requests.RequestException as e:
            log.info("attempt %s term=%s type=%s page=%d POST %s -> network error %s (%d ms)", tag, term,
                     shown_type, page + 1, url, e.__class__.__name__, (time.perf_counter() - t0) * 1000)
            return _Page(stop=CallerInfo(error=f"network: {e.__class__.__name__}"))
        dump = self._dump(f"{tag.replace('/', 'of')}_p{page + 1}", r) if self.cfg.get("debug_dump") else ""
        log.info("attempt %s term=%s type=%s page=%d -> HTTP %s POST %s %s (%d ms)%s", tag, term,
                 shown_type, page + 1, r.status_code, r.url, self._summarize(r),
                 (time.perf_counter() - t0) * 1000, f" dump={dump}" if dump else "")
        if self._is_login_url(r.url) or r.status_code in (401, 403):
            return _Page(stop=CallerInfo(error="MiniCRM session expired"))
        if r.status_code in (400, 404):
            return _Page(problem=f"HTTP {r.status_code} from {url}")
        if r.status_code != 200:
            return _Page(stop=CallerInfo(error=f"HTTP {r.status_code}"))
        try:
            data = r.json()
        except ValueError:                       # HTML (login / error page) instead of JSON
            return _Page(problem=f"unexpected response, not JSON ({r.headers.get('Content-Type', '?')})")
        items, err = self._items_of(data)
        if err:
            return _Page(stop=err)
        return _Page(items=items, data=data)

    @staticmethod
    def _items_of(data):
        """{"Rpc/List": {"Status":200, "Body": {"Items":[...]}}} -> (items, None) or (None, error).
        A reply we cannot read is an error (field names only), never 'unknown'."""
        node = _ci(data, "Rpc/List")
        if not isinstance(node, dict):
            keys = sorted(data)[:6] if isinstance(data, dict) else type(data).__name__
            return None, CallerInfo(error=f"unexpected JSON, keys={keys}")
        status = _ci(node, "Status")
        if status in (401, 403):
            return None, CallerInfo(error="MiniCRM session expired")
        if status not in (None, 200):
            return None, CallerInfo(error=f"Rpc/List status {status}")
        items = _ci(_ci(node, "Body"), "Items")
        if not isinstance(items, list):
            return None, CallerInfo(error="unexpected JSON: no Rpc/List > Body > Items list")
        return items, None

    def _link(self, item, term: str) -> str:
        """Url is a browser route like "#AddressBook/21157"; without it open the search page instead."""
        link = _ci(item, "Url")
        if isinstance(link, str) and link.strip():
            return urljoin(self._site(), link)
        return self._site() + "#Search?Query=" + quote(term, safe="")

    @staticmethod
    def _company_of(item) -> tuple[str, str]:
        """(company, field it came from) from a Person item, or ("", ""). A field may be a plain string, an
        object with a Name ({"Business": {"Id": 1, "Name": "X Kft."}}) or a list of those (the first wins)."""
        for key in _COMPANY_KEYS:
            company = _company_value(_ci(item, key))
            if company:
                return company, key
        return "", ""

    @staticmethod
    def _same_name(a: str, b: str) -> bool:
        """Same person name: ignores case and spacing, but not accents ("Kis István" != "Kis Istvan")."""
        norm = lambda x: " ".join(unicodedata.normalize("NFC", x).casefold().split())
        return norm(a) == norm(b)

    def _person_from_items(self, items, term: str) -> CallerInfo | None:
        """The FIRST Person with a name is the caller shown on the card (immediately). Further Person hits are
        kept in `alternates`: stage 2 may find the company on one of them (a private and a business profile of
        the same caller). A company already present in a search item is used right away (a plain search
        result usually has none: Content = e-mail + phone)."""
        people = [(_clean(_ci(i, "Name")), i) for i in items if _is_person(i)]
        people = [(n, i) for n, i in people if n]
        if not people:
            return None
        name, first = people[0]
        company, key = self._company_of(first)
        first_link = self._link(first, term)
        alternates, seen = [], {first_link}
        for n, i in people[1:]:
            link = self._link(i, term)
            if link in seen:                       # the same profile listed twice
                continue
            seen.add(link)
            alternates.append((n, link))
            if not company and (self._same_name(n, name) or not self.cfg.get("company_same_name", True)):
                company, key = self._company_of(i)
        # field NAMES only, never values: shows what MiniCRM really sends for a person
        log.info("person item fields=%s company=%s persons=%d", sorted(str(k) for k in first),
                 f"from {key}" if company else "none", 1 + len(alternates))
        return CallerInfo(True, name, company, first_link, alternates=alternates)

    def _parse_batch(self, data, term: str) -> CallerInfo:
        """First Person of one response; no such item = not found; unreadable reply = error."""
        items, err = self._items_of(data)
        if err:
            return err
        return self._person_from_items(items, term) or CallerInfo(found=False)

    # ---- stage 2: the company, fetched AFTER the name is already on the card ----------------------
    def company_enabled(self) -> bool:
        """Stage 2 needs the detail request from config.json ("company_request"); without it, it is off."""
        return (not self.cfg["mock"] and bool(self.cfg.get("company_lookup", True))
                and bool(self.cfg.get("company_request")))

    @staticmethod
    def _person_id(profile_url: str) -> str:
        m = _COMPANY_ID.search(profile_url or "")
        return m.group(1) if m else ""

    def _company_form(self, pid: str) -> dict:
        """Form data of the detail request: config "company_request" (dict, or the raw text copied from
        DevTools) with {id} replaced by the contact id."""
        raw = self.cfg.get("company_request")
        pairs = parse_qsl(raw, keep_blank_values=True) if isinstance(raw, str) else list(dict(raw).items())
        return {str(k).replace("{id}", pid): str(v).replace("{id}", pid) for k, v in pairs}

    @staticmethod
    def _walk_path(data, path: str):
        """"Rpc/X > Body > Business > 0 > Name": dict keys (any case) and list positions."""
        node = data
        for token in (t.strip() for t in path.split(">")):
            if isinstance(node, list):
                node = node[int(token)] if token.isdigit() and int(token) < len(node) else None
            else:
                node = _ci(node, token)
            if node is None:
                return None
        return node

    def _find_company(self, data, pid: str = "") -> tuple[str, str]:
        """(company, where it came from) in a detail answer. With "company_path" exactly there ({id} = the
        contact id, because the answer is keyed like "Rpc/Account/MainInfo/<id>"); otherwise the shallowest field
        called like _COMPANY_KEYS. The first company wins when there are several."""
        path = str(self.cfg.get("company_path") or "").strip().replace("{id}", pid)
        if path:
            company = _company_value(self._walk_path(data, path))
            return (company, "company_path") if company else ("", "")
        queue = deque([(data, 0)])
        while queue:
            node, depth = queue.popleft()
            if isinstance(node, dict):
                for key in _COMPANY_KEYS:
                    company = _company_value(_ci(node, key))
                    if company:
                        return company, key
                children = [v for v in node.values() if isinstance(v, (dict, list))]
            elif isinstance(node, list):
                children = [v for v in node[:20] if isinstance(v, (dict, list))]
            else:
                continue
            if depth < 6:
                queue.extend((v, depth + 1) for v in children)
        return "", ""

    @staticmethod
    def _key_paths(data, limit: int = 60) -> list:
        """Field NAMES of an answer ("Rpc/X > Body > Business > Name", lists as "Key[]"), never values.
        Logged when no company was found, so the right "company_path" can be picked."""
        out = []

        def rec(node, prefix, depth):
            if len(out) >= limit or depth > 6 or not isinstance(node, dict):
                return
            for key, value in node.items():
                p = f"{prefix} > {key}" if prefix else str(key)
                if isinstance(value, dict):
                    rec(value, p, depth + 1)
                elif isinstance(value, list):
                    rec(value[0] if value else None, p + "[]", depth + 1)
                    if not value or not isinstance(value[0], dict):
                        out.append(p + "[]")
                else:
                    out.append(p)
        rec(data, "", 0)
        return out[:limit]

    def _company_get(self, pid: str):
        entry = self._company_cache.get(pid)
        if entry is None:
            return None
        expires, company = entry
        if time.monotonic() >= expires:
            self._company_cache.pop(pid, None)
            return None
        return company

    def fetch_company(self, info: CallerInfo) -> str:
        """Company of a found caller. Runs in a background thread AFTER the name is on the card. Tries the shown
        profile first, then the other Person hits for the same number (in search order, at most
        COMPANY_MAX_CANDIDATES, one detail request each, cached per contact id) and returns the first company.
        By default only candidates with the same name count ("company_same_name"). Never raises; any problem
        returns "" and is only logged (a missing company line is not an error the call card should show)."""
        if not self.company_enabled():
            return ""
        candidates = [(info.name, info.profile_url)] + list(info.alternates)
        same_only = bool(self.cfg.get("company_same_name", True))
        seen, tried, skipped = set(), 0, 0
        for name, url in candidates:
            pid = self._person_id(url)
            if not pid or pid in seen:
                continue
            seen.add(pid)
            if same_only and not self._same_name(name, info.name):
                skipped += 1
                continue
            if tried >= self.COMPANY_MAX_CANDIDATES:
                break
            tried += 1
            company, ok = self._company_for_id(pid)
            if company:
                if tried > 1:
                    log.info("company: candidate %d of %d had it (id=%s)", tried, len(candidates), pid)
                return company
            if not ok:             # network / session problem: the other candidates would fail the same way
                break
        if skipped:
            log.info("company: skipped %d candidate(s) with a different name (company_same_name)", skipped)
        return ""

    def _company_for_id(self, pid: str) -> tuple[str, bool]:
        """(company or "", ok). ok=False = the request failed (nothing cached, retried next call);
        ok=True with "" = this contact has no company (cached for NOT_FOUND_TTL_S)."""
        t0 = time.perf_counter()
        cached = self._company_get(pid)
        if cached is not None:
            log.info("company id=%s -> found=%s (0 ms, cached)", pid, bool(cached))
            return cached, True
        ms = lambda: (time.perf_counter() - t0) * 1000
        try:
            r = self.http.post(self._batch_url(), data=self._company_form(pid), headers=self._ajax_headers(),
                               timeout=min(3, self.cfg["timeout_s"]), allow_redirects=True)
        except requests.RequestException as e:
            log.info("company id=%s fetch failed: network %s (%d ms)", pid, e.__class__.__name__, ms())
            return "", False
        except Exception as e:                    # a malformed company_request in config.json
            log.warning("company id=%s fetch failed: bad company_request (%s)", pid, e.__class__.__name__)
            return "", False
        if self.cfg.get("debug_dump"):
            self._dump(f"company_{pid}", r, latest=False)
        if self._is_login_url(r.url) or r.status_code in (401, 403):
            log.info("company id=%s fetch failed: MiniCRM session expired (%d ms)", pid, ms())
            return "", False
        if r.status_code != 200:
            log.info("company id=%s fetch failed: HTTP %s (%d ms)", pid, r.status_code, ms())
            return "", False
        try:
            data = r.json()
        except ValueError:
            log.info("company id=%s fetch failed: not JSON (%d ms)", pid, ms())
            return "", False
        company, source = self._find_company(data, pid)
        ttl = self.COMPANY_TTL_S if company else self.NOT_FOUND_TTL_S
        self._company_cache[pid] = (time.monotonic() + ttl, company)
        if company:
            log.info("company id=%s -> found=True from %s (%d ms)", pid, source, ms())
        else:
            log.info("company id=%s -> none (%d ms) fields=%s", pid, ms(), self._key_paths(data))
        return company, True

    # ---- fallback: the contact named in the sub-line of deal cards ------------------------------
    @staticmethod
    def _texts(item) -> list:
        """String fields of a result card that may hold the sub-line ("Content" first)."""
        if not isinstance(item, dict):
            return []
        out = []
        content = _ci(item, "Content")
        if isinstance(content, str):
            out.append(content)
        out += [v for k, v in item.items() if isinstance(v, str) and str(k).lower() not in _SKIP_FIELDS]
        return out

    def _name_from_deals(self, items, digits: str, term: str) -> CallerInfo | None:
        """Sub-line like "Kis István, 06305256058": the name is the segment just before the one that holds
        the caller's number. Used only when exactly one distinct name comes out; several = ambiguous
        (e.g. a switchboard number) and a wrong name on the call card is worse than none."""
        tail = digits[-9:]
        names, matching, total = {}, 0, 0
        for item in items:
            if _is_person(item):
                continue
            total += 1
            hit = False
            for text in self._texts(item):
                parts = [_clean(p) for p in _SEGMENT.split(re.sub(r"(?i)<br\s*/?>", ",", text))]
                for i, part in enumerate(parts):
                    if i and tail in re.sub(r"\D", "", part):
                        hit = True
                        if _plausible_name(parts[i - 1]):
                            names.setdefault(parts[i - 1].casefold(), (parts[i - 1], item))
                        break
            matching += hit
        verdict = "used" if len(names) == 1 else ("ambiguous" if names else "none")
        log.info("deal fallback: %d non-person item(s), %d with the caller's number, %d distinct name(s) -> %s",
                 total, matching, len(names), verdict)
        if len(names) != 1:
            return None
        display, item = next(iter(names.values()))
        return CallerInfo(True, display, "", self._link(item, term))

    # ---- diagnostics -----------------------------------------------------------------------
    @staticmethod
    def _type_counts(items) -> dict:
        counts = {}
        for i in items:
            t = str(_ci(i, "Type")) if isinstance(i, dict) else "?"
            counts[t] = counts.get(t, 0) + 1
        return counts

    @classmethod
    def _summarize(cls, r) -> str:
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
                            parts.append(f"items={len(items)} types={cls._type_counts(items)}")
                        body = _ci(node, "Body")
                        if isinstance(body, dict):       # field names show whether a page/total count exists
                            parts.append(f"body keys={sorted(body)[:8]}")
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
    def _dump(label: str, r, latest: bool = True) -> str:
        """debug_dump: save each attempt's raw response next to app.log (contains customer data!).
        last_lookup_<label>.<ext> per attempt, plus last_lookup.<ext> = the most recent one."""
        ext = ".json" if "json" in r.headers.get("Content-Type", "").lower() else ".html"
        try:
            d = config.data_dir()
            (d / f"last_lookup_{label}{ext}").write_text(r.text, encoding="utf-8")
            if latest:
                (d / f"last_lookup{ext}").write_text(r.text, encoding="utf-8")
            return f"last_lookup_{label}{ext}"
        except OSError as e:
            log.warning("debug dump failed: %s", e)
            return ""
