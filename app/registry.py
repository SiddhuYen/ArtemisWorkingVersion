"""State business-registry lookup — the records silo.

Why this exists as code rather than as an instruction to the model.

Routing regional people fails on a specific, reproducible gap: the strongest tie
a local figure has is usually a corporate filing naming them alongside somebody
else, and that filing is invisible to every search engine. It sits behind a
query form under an opaque numeric id, so a web search for the person returns
their organisation's homepage and a directory listing, never the record. A model
searching honestly concludes "no named individual is tied to them", which is a
true statement about search results and a false one about the public record.

Asking the model to fetch the registry instead is unreliable — it has to guess
the URL shape, remember to do it for every organisation, and spend tool calls on
it. So this module does the lookup deterministically before the model runs, and
hands it the names.

Best-effort throughout: a registry that is down, slow, or has changed its markup
yields no officers and never raises into the caller. A route that would have
worked without this must still work when it returns nothing.

THE INVARIANT, which every state parser must preserve: an entity only counts if
the person we searched for is NAMED on that filing. An org-name guess is loose
on purpose, so a search for "Marshall University" turns up the alumni
association and the real-estate foundation — real entities with real officers,
none of whom share a filing with this person. Returning their names would assert
a tie that does not exist, which is worse than returning nothing, because the
payload is presented to the model as attested evidence.

ACCESS REALITY (measured 2026-08-09, see scripts/registry_hitrate.py and the
census in the audit notes): most state registries are NOT reachable by an HTTP
client. Of 40 states probed, 14 sit behind Cloudflare/WAF interstitials, 7
require a captcha, 4 are paywalled subscriber logins, 3 are JS-only SPAs, and 7
did not resolve. Delaware additionally does not publish officers or directors at
all — only the registered agent — so it is useless here even if the captcha were
solved. Adding a state is cheap ONLY when that state answers plain HTTP; that is
the exception, not the rule.

Adding a state is one REGISTRIES entry. Each entry owns its own request flow,
because the flows genuinely differ: WV answers a single GET, RI needs an
ASP.NET postback dance. Both must respect the shared throttle.
"""
from __future__ import annotations

import html
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

import httpx

from . import scraped_names

log = logging.getLogger(__name__)

# Registries are public services run on modest hardware. Keep the request count
# per build small and the timeouts short: this is an enrichment step, and it is
# never worth delaying or failing a route over.
#
# _TIMEOUT is per request. WV's name search was measured at 9.7s once during an
# audit (cold) and 1.6s warm; RI's postback chain runs 0.3-1.5s per leg. 12s
# accommodates the observed cold case with headroom. If a state is added whose
# search regularly exceeds this, raise it FOR THAT STATE via Registry.timeout
# rather than globally — a slow state must not slow every other state down.
_TIMEOUT = 12.0
_MAX_ORGS_PER_ENDPOINT = 3
_MAX_ENTITIES_PER_ORG = 2

# Politeness. There was previously no delay at all: requests fired back to back
# at a state government host. One second between requests to the SAME host, and
# a hard cap on how many requests one build may make to that host, so a single
# route can never turn into a crawl.
_HOST_DELAY_S = 1.0
# Sized from the most expensive implemented flow so the cap bounds runaway
# behaviour without silently truncating a legitimate lookup:
#   WV  1 search + 2 records            = 3 per org  -> 3 orgs =  9
#   RI  3 postbacks + 2 records         = 5 per org  -> 3 orgs = 15
# A cap of 12 quietly cut every RI lookup short at two orgs and returned fewer
# co-officers than the record actually holds — a silent under-report, which is
# the worst kind. 20 covers RI's worst case with headroom.
#
# Cost of the cap being hit: at _HOST_DELAY_S it also sets a latency floor of
# ~20s per endpoint against one host. That is why _MAX_ORGS_PER_ENDPOINT stays
# at 3 — widening org guessing multiplies straight into wall-clock.
_HOST_REQUEST_CAP = 20

# Identify the tool honestly. This previously spoofed Chrome, which misstates
# who is calling and removes the operator's ability to identify or rate-limit
# us. Verified 2026-08-09: apps.sos.wv.gov returns byte-equivalent results for
# this UA and for the Chrome string it replaced, so honesty costs nothing here.
_UA = ("ArtemisRegistryBot/1.0 "
       "(+https://github.com/SiddhuYen/artemis; contact: siddhuy.2008@gmail.com)")


class HostBudgetExceeded(Exception):
    """This build has spent its request allowance for one host."""


@dataclass
class Fetcher:
    """One build's HTTP allowance, shared by every registry call in that build.

    Owns the politeness delay and the per-host cap. Created per co_officers()
    call so the cap is per build, not process-global.
    """

    client: httpx.Client
    delay_s: float = _HOST_DELAY_S
    cap: int = _HOST_REQUEST_CAP
    counts: dict[str, int] = field(default_factory=dict)
    _last: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    calls: list[dict] = field(default_factory=list)   # observability for the harness

    def _gate(self, host: str) -> None:
        with self._lock:
            n = self.counts.get(host, 0)
            if n >= self.cap:
                raise HostBudgetExceeded(f"{host}: {n} requests already this build")
            self.counts[host] = n + 1
            last = self._last.get(host)
            now = time.monotonic()
            wait = 0.0 if last is None else max(0.0, self.delay_s - (now - last))
            self._last[host] = now + wait
        if wait:
            time.sleep(wait)

    def _record(self, method: str, url: str, status: int | None,
                secs: float, err: str | None) -> None:
        self.calls.append({"method": method, "url": url, "status": status,
                           "secs": round(secs, 3), "error": err})

    def get(self, url: str, *, timeout: float = _TIMEOUT, **kw) -> httpx.Response:
        host = urlsplit(url).netloc
        self._gate(host)
        t0 = time.monotonic()
        try:
            r = self.client.get(url, timeout=timeout, follow_redirects=True,
                                headers={"User-Agent": _UA}, **kw)
            self._record("GET", url, r.status_code, time.monotonic() - t0, None)
            r.raise_for_status()
            return r
        except Exception as exc:
            self._record("GET", url, getattr(getattr(exc, "response", None),
                                             "status_code", None),
                         time.monotonic() - t0, f"{type(exc).__name__}")
            raise

    def post(self, url: str, *, data: dict, timeout: float = _TIMEOUT) -> httpx.Response:
        host = urlsplit(url).netloc
        self._gate(host)
        t0 = time.monotonic()
        try:
            r = self.client.post(url, data=data, timeout=timeout,
                                 follow_redirects=True,
                                 headers={"User-Agent": _UA})
            self._record("POST", url, r.status_code, time.monotonic() - t0, None)
            r.raise_for_status()
            return r
        except Exception as exc:
            self._record("POST", url, getattr(getattr(exc, "response", None),
                                              "status_code", None),
                         time.monotonic() - t0, f"{type(exc).__name__}")
            raise


def new_fetcher(client: httpx.Client | None = None) -> Fetcher:
    return Fetcher(client=client or httpx.Client())


@dataclass(frozen=True)
class Officer:
    """One person named in an official capacity on one filing."""
    name: str
    role: str          # Manager, Member, Organizer, Incorporator, Agent, President, ...
    org_name: str
    org_id: str
    state: str
    source_url: str


@dataclass(frozen=True)
class Registry:
    state: str
    # org name -> [(entity_id, entity_name)]; owns its own request flow
    find_entities: Callable[[Fetcher, str], list[tuple[str, str]]]
    record_url: str                                   # {org_id}
    parse_record: Callable[[str], list[tuple[str, str]]]   # html -> [(role, name)]
    access_note: str = ""


# --- shared html helpers -----------------------------------------------------
def _text(fragment: str) -> str:
    """Strip tags and collapse whitespace, preserving nothing but the words."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _first_line(cell: str) -> str:
    """The name out of a NAME<br/>street<br/>city cell."""
    return _text(re.split(r"<br\s*/?>", cell, maxsplit=1)[0])


# Filings routinely carry a literal placeholder where a name should be. WV in
# particular emits "NOT LISTED", "NONE LISTED", "SAME AS PRES.", "AND OTHER".
# These are not people. Passing one through would hand the model a fabricated
# human tagged as registry-attested evidence — precisely the failure the
# named-on-the-filing invariant exists to prevent, arriving by a different door.
#
# The rules live in app/scraped_names.py so there is exactly one boundary where
# scraped text becomes a name, shared by every state parser.
_is_placeholder_name = scraped_names.is_placeholder


# --- West Virginia -----------------------------------------------------------
# robots.txt verified 2026-08-09. User-agent: * disallows 8 paths:
#   /adlaw/executivejournal/senateconfirmation/, /business/service-of-process/Packaged,
#   /business/service-of-process/Packaged/, /email/, /Templates/, /universalaccess/,
#   /business/corporations/readpdf.aspx, /business/charities/readpdf.aspx
# The two paths used here — /business/corporations/ (name search) and
# /business/corporations/organization.aspx (entity record) — are NOT disallowed.
# Note readpdf.aspx in the same directory IS disallowed and is never fetched.
_WV_SEARCH = ("https://apps.sos.wv.gov/business/corporations/"
              "?SearchType=Name&Search={query}")
_WV_RECORD = ("https://apps.sos.wv.gov/business/corporations/"
              "organization.aspx?org={org_id}")
_WV_SEARCH_ROW = re.compile(r'organization\.aspx\?org=(\d+)[^>]*>(.*?)</a>', re.I | re.S)
_WV_OFFICER_TABLE = re.compile(r'<h2>\s*Officers\s*</h2>(.*?)</table>', re.I | re.S)
_WV_OFFICER_ROW = re.compile(r'<tr>\s*<th>(.*?)</th>\s*<td>(.*?)</td>\s*</tr>', re.I | re.S)


def _wv_parse_search(page: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for org_id, label in _WV_SEARCH_ROW.findall(page):
        name = _text(label)
        if name and (org_id, name) not in out:
            out.append((org_id, name))
    return out


def _wv_find_entities(f: Fetcher, org_name: str) -> list[tuple[str, str]]:
    q = httpx.QueryParams({"q": org_name})["q"].replace(" ", "+")
    return _wv_parse_search(f.get(_WV_SEARCH.format(query=q)).text)


def _wv_parse_record(page: str) -> list[tuple[str, str]]:
    block = _WV_OFFICER_TABLE.search(page)
    if not block:
        return []
    out: list[tuple[str, str]] = []
    for role, cell in _WV_OFFICER_ROW.findall(block.group(1)):
        role_t, name_t = _text(role), _first_line(cell)
        # The header row ("Type" / "Name/Address") matches the row shape too.
        if not name_t or role_t.lower() in {"type", ""}:
            continue
        clean = scraped_names.clean_person_name(name_t, context=f"WV role={role_t}")
        if clean is None:
            continue
        out.append((role_t, clean))
    return out


# --- Rhode Island ------------------------------------------------------------
# robots.txt verified 2026-08-09: https://business.sos.ri.gov/robots.txt returns
# HTTP 404 — no robots.txt is published, so no path is disallowed (RFC 9309
# §2.3.1.3: an "unavailable" 4xx status means the crawler may access resources).
#
# RI is an ASP.NET WebForms app and needs three requests to run one name search:
#   1. GET  CorpSearch.aspx                  -> viewstate + the search radios
#   2. POST __EVENTTARGET=CorpSearch$0       -> select "by entity name" (postback)
#   3. POST txtEntityName + btnSearch        -> CorpSearchResults.aspx
# then one GET per entity record. That is 3 + N requests against WV's 1 + N,
# which is why the per-host cap matters.
_RI_SEARCH = "https://business.sos.ri.gov/CorpWeb/CorpSearch/CorpSearch.aspx"
_RI_RECORD = ("https://business.sos.ri.gov/CorpWeb/CorpSearch/CorpSummary.aspx"
              "?FEIN={org_id}&SEARCH_TYPE=1")
_RI_RESULT_ROW = re.compile(r'CorpSummary\.aspx\?FEIN=(\d+)[^>]*>(.*?)</a>', re.I | re.S)
_RI_OFFICER_GRID = re.compile(
    r'<table[^>]*id="MainContent_grdOfficers".*?</table>', re.I | re.S)
_RI_TR = re.compile(r'<tr[^>]*>(.*?)</tr>', re.I | re.S)
_RI_TD = re.compile(r'<t[dh][^>]*>(.*?)</t[dh]>', re.I | re.S)
_RI_AGENT = re.compile(r'id="MainContent_lblResidentAgentName"[^>]*>(.*?)</', re.I | re.S)
# Drop the junk-input fields belonging to the embedded Google site-search form;
# posting them back confuses the WebForms handler.
_RI_DROP = {"q", "btnG", "client", "site", "output", "proxystylesheet"}


def _aspnet_form(page: str) -> dict[str, str]:
    """Every input name=value plus each select's selected option.

    ASP.NET rejects a partial postback with a 500, so the whole form has to be
    echoed back, not just __VIEWSTATE.
    """
    d: dict[str, str] = {}
    for tag in re.findall(r"<input[^>]+>", page):
        n = re.search(r'name="([^"]+)"', tag)
        if not n:
            continue
        typ = (re.search(r'type="([^"]+)"', tag) or [None, "text"])[1].lower()
        if typ in ("submit", "button", "image", "reset"):
            continue
        if typ in ("checkbox", "radio") and "checked" not in tag.lower():
            continue
        v = re.search(r'value="([^"]*)"', tag)
        d[html.unescape(n.group(1))] = html.unescape(v.group(1)) if v else ""
    for m in re.finditer(r'<select[^>]+name="([^"]+)"(.*?)</select>', page, re.S | re.I):
        opt = (re.search(r'<option[^>]*selected[^>]*value="([^"]*)"', m.group(2), re.I)
               or re.search(r'<option[^>]*value="([^"]*)"', m.group(2), re.I))
        d[html.unescape(m.group(1))] = html.unescape(opt.group(1)) if opt else ""
    for junk in _RI_DROP:
        d.pop(junk, None)
    return d


def _ri_find_entities(f: Fetcher, org_name: str) -> list[tuple[str, str]]:
    page = f.get(_RI_SEARCH).text
    d = _aspnet_form(page)
    d.update({"__EVENTTARGET": "ctl00$MainContent$CorpSearch$0",
              "__EVENTARGUMENT": "",
              "ctl00$MainContent$CorpSearch": "rdoByEntityName"})
    page2 = f.post(_RI_SEARCH, data=d).text
    d2 = _aspnet_form(page2)
    d2.update({"__EVENTTARGET": "", "__EVENTARGUMENT": "",
               "ctl00$MainContent$CorpSearch": "rdoByEntityName",
               "ctl00$MainContent$txtEntityName": org_name,
               # B=Begins with, M=Exact, F=Full text, S=Soundex. "Begins with"
               # keeps the entity set tight; the named-on-the-filing check is
               # what actually protects correctness, but a tighter search costs
               # fewer record fetches.
               "ctl00$MainContent$ddBeginsWithEntityName": "B",
               "ctl00$MainContent$btnSearch": "Search"})
    out: list[tuple[str, str]] = []
    for org_id, label in _RI_RESULT_ROW.findall(f.post(_RI_SEARCH, data=d2).text):
        name = _text(label)
        if name and (org_id, name) not in out:
            out.append((org_id, name))
    return out


def _ri_parse_record(page: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    grid = _RI_OFFICER_GRID.search(page)
    if grid:
        for tr in _RI_TR.findall(grid.group(0)):
            cells = [_text(c) for c in _RI_TD.findall(tr)]
            if len(cells) < 2:
                continue
            role, name = cells[0], cells[1]
            if not name or role.lower() in {"title", ""}:
                continue
            clean = scraped_names.clean_person_name(name, context=f"RI role={role}")
            if clean is None:
                continue
            out.append((role, clean))
    # The resident/registered agent is published separately and is NOT an
    # officer. It is returned so the caller can see it, tagged with a role that
    # makes it identifiable as an agent rather than a colleague — a registered
    # agent is a service provider, not a warm path.
    agent = _RI_AGENT.search(page)
    if agent:
        nm = scraped_names.clean_person_name(_text(agent.group(1)),
                                             context="RI registered agent")
        if nm is not None:
            out.append(("Registered Agent", nm))
    return out


REGISTRIES: dict[str, Registry] = {
    "WV": Registry(
        state="WV",
        find_entities=_wv_find_entities,
        record_url=_WV_RECORD,
        parse_record=_wv_parse_record,
        access_note="plain GET name search; robots.txt allows both paths",
    ),
    "RI": Registry(
        state="RI",
        find_entities=_ri_find_entities,
        record_url=_RI_RECORD,
        parse_record=_ri_parse_record,
        access_note="ASP.NET postback x3 then GET record; no robots.txt published",
    ),
}

# States probed on 2026-08-09 and deliberately NOT implemented, with the reason.
# Kept in code so the next person does not re-derive it. See the module
# docstring for the aggregate.
UNAVAILABLE: dict[str, str] = {
    "DE": "captcha on NameSearch.aspx AND Delaware publishes no officers/directors, "
          "only the registered agent — useless for co-officer extraction",
    "TX": "SOSDirect is a paid subscriber login (direct.sos.state.tx.us/acct/acct-login.asp)",
    "FL": "search.sunbiz.org returns a Cloudflare interstitial (HTTP 403 'Just a moment')",
    "OH": "HTTP 403 / maintenance page",
    "CO": "Cloudflare interstitial",
    "GA": "Cloudflare interstitial",
    "WA": "JS-only SPA; its JSON API answers 'System verification in progress'",
    "NC": "Cloudflare interstitial",
    "OR": "captcha gate on the search page",
    "IA": "Cloudflare AND robots.txt disallows the search path",
    "VA": "captcha AND robots.txt disallows /",
    "AL": "robots.txt disallows /",
    "LA": "paywalled AND robots.txt disallows the search path",
    "AK": "robots.txt disallows /cbp/",
    "MO": "Telerik AJAX WebForms; not driveable by plain form posts",
}


# --- reading state and organisation out of free-text context -----------------
_STATE_NAMES = {
    "west virginia": "WV", "virginia": "VA", "ohio": "OH", "kentucky": "KY",
    "pennsylvania": "PA", "maryland": "MD", "california": "CA", "new york": "NY",
    "texas": "TX", "florida": "FL", "delaware": "DE", "illinois": "IL",
    "massachusetts": "MA", "washington": "WA", "georgia": "GA", "michigan": "MI",
    "north carolina": "NC", "colorado": "CO", "tennessee": "TN", "arizona": "AZ",
    "rhode island": "RI",
}

# Capitalised runs that are never the organisation we want to look up.
_ORG_STOPWORDS = {
    "university", "college", "school", "governor", "senator", "president",
    "director", "board", "member", "founder", "executive", "state", "county",
    "city", "north", "south", "east", "west", "the", "and", "of", "at", "in",
}
_ORG_RUN = re.compile(r"\b([A-Z][A-Za-z&.'-]*(?:\s+[A-Z][A-Za-z&.'-]*){1,6})")


def state_from_text(text: str) -> str | None:
    """Best-effort state code from free text. Longest name first, so
    'West Virginia' is not read as 'Virginia'."""
    low = (text or "").lower()
    for name in sorted(_STATE_NAMES, key=len, reverse=True):
        if name in low:
            return _STATE_NAMES[name]
    for code in set(_STATE_NAMES.values()):
        if re.search(rf"\b{code}\b", text or ""):
            return code
    return None


def org_candidates(text: str, limit: int = _MAX_ORGS_PER_ENDPOINT) -> list[str]:
    """Organisation-looking phrases from an endpoint's context string.

    Deliberately loose: a wrong guess costs one cheap request that returns no
    matches, while a missed organisation costs the hop. Runs that are entirely
    generic words are dropped, and a parenthesised acronym like "(HAWC)" is not
    searched on its own — registries index full legal names.
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for run in _ORG_RUN.findall(text):
        phrase = run.strip(" .,;")
        words = [w for w in phrase.split() if w.lower() not in _ORG_STOPWORDS]
        if len(phrase.split()) < 2 or not words:
            continue
        key = phrase.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(phrase)
        if len(out) >= limit:
            break
    return out


# --- lookup ------------------------------------------------------------------
def officers_for_org(org_name: str, state: str, *,
                     fetcher: Fetcher | None = None,
                     client: httpx.Client | None = None) -> list[Officer]:
    """Every person named in an official capacity on filings matching `org_name`."""
    reg = REGISTRIES.get((state or "").upper())
    if not reg or not org_name:
        return []

    own = fetcher or new_fetcher(client)
    close = fetcher is None and client is None
    try:
        try:
            entities = reg.find_entities(own, org_name)
        except HostBudgetExceeded as exc:
            log.info("registry budget spent for %r in %s: %s", org_name, state, exc)
            return []
        except Exception as exc:                     # network, 4xx/5xx, timeout
            log.debug("registry search failed for %r in %s: %s", org_name, state, exc)
            return []

        found: list[Officer] = []
        for org_id, entity_name in entities[:_MAX_ENTITIES_PER_ORG]:
            url = reg.record_url.format(org_id=org_id)
            try:
                record = own.get(url).text
            except HostBudgetExceeded:
                break
            except Exception as exc:
                log.debug("registry record %s failed: %s", org_id, exc)
                continue
            for role, name in reg.parse_record(record):
                found.append(Officer(name=name, role=role, org_name=entity_name,
                                     org_id=org_id, state=reg.state, source_url=url))
        return found
    finally:
        if close:
            own.client.close()


def co_officers(person: str, context: str, *,
                fetcher: Fetcher | None = None,
                client: httpx.Client | None = None) -> list[dict[str, str]]:
    """Registry-attested people who share a filing with `person`.

    `context` is the endpoint's context string — it supplies both the state and
    the organisations to look up. Returns plain dicts because this rides into the
    model payload as JSON. The endpoint themselves is filtered out: the useful
    output is who ELSE is on the filing.
    """
    state = state_from_text(context)
    if not state or state.upper() not in REGISTRIES:
        return []

    person_key = _norm(person)
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    own = fetcher or new_fetcher(client)
    close = fetcher is None and client is None
    try:
        for org in org_candidates(context):
            # Group by entity, because the co-filing claim is per FILING. An
            # organisation guess is loose on purpose, so a search for "Marshall
            # University" returns the alumni association and the real-estate
            # foundation — real entities with real officers, none of whom share
            # a filing with this person. Returning their names would assert a
            # tie that does not exist, which is worse than returning nothing:
            # the payload is presented to the model as attested evidence.
            # So an entity counts only when the person is named on it.
            by_entity: dict[str, list[Officer]] = {}
            for off in officers_for_org(org, state, fetcher=own):
                by_entity.setdefault(off.org_id, []).append(off)

            for officers in by_entity.values():
                if not any(_norm(o.name) == person_key for o in officers):
                    continue                       # person is not on this filing
                for off in officers:
                    if _norm(off.name) == person_key:
                        continue                   # the endpoint's own row
                    key = (_norm(off.name), off.org_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({
                        "name": off.name,
                        "role": off.role,
                        "organization": off.org_name,
                        "state": off.state,
                        "source_url": off.source_url,
                    })
    except Exception as exc:                      # never break a build over this
        log.warning("registry lookup for %r aborted: %s", person, exc)
    finally:
        if close:
            own.client.close()
    return out


def _norm(name: str) -> str:
    """Compare names ignoring case, punctuation and honorifics."""
    n = re.sub(r"[^a-z ]", " ", (name or "").lower())
    n = re.sub(r"\b(dr|mr|mrs|ms|jr|sr|ii|iii|iv)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()
