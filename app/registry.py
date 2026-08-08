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

Two GETs per organisation:

    1. the name search        -> entity ids
    2. the entity record      -> every person named in an official capacity

Both are public, unauthenticated, and answer plain GET requests. Best-effort
throughout: a registry that is down, slow, or has changed its markup yields no
officers and never raises into the caller. A route that would have worked
without this must still work when it returns nothing.

Adding a state is one REGISTRIES entry. The markup differs per state, so each
entry carries its own parser hook; WV is the one verified against live pages.
"""
from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from typing import Callable

import httpx

log = logging.getLogger(__name__)

# Registries are public services run on modest hardware. Keep the request count
# per build small and the timeouts short: this is an enrichment step, and it is
# never worth delaying or failing a route over.
_TIMEOUT = 12.0
_MAX_ORGS_PER_ENDPOINT = 3
_MAX_ENTITIES_PER_ORG = 2
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")


@dataclass(frozen=True)
class Officer:
    """One person named in an official capacity on one filing."""
    name: str
    role: str          # Manager, Member, Organizer, Incorporator, Agent, ...
    org_name: str
    org_id: str
    state: str
    source_url: str


@dataclass(frozen=True)
class Registry:
    state: str
    search_url: str    # {query} -> url-encoded organisation name
    record_url: str    # {org_id}
    parse_search: Callable[[str], list[tuple[str, str]]]   # html -> [(org_id, name)]
    parse_record: Callable[[str], list[tuple[str, str]]]   # html -> [(role, name)]


# --- West Virginia -----------------------------------------------------------
# Verified against live pages. The search result table carries one
# `organization.aspx?org=<id>` link per match with the entity name as link text;
# the record page puts officers in a table headed <h2>Officers</h2> as
# <tr><th>Role</th><td>NAME<br/>address...</td></tr>.
_WV_SEARCH_ROW = re.compile(
    r'organization\.aspx\?org=(\d+)[^>]*>(.*?)</a>', re.I | re.S)
_WV_OFFICER_TABLE = re.compile(
    r'<h2>\s*Officers\s*</h2>(.*?)</table>', re.I | re.S)
_WV_OFFICER_ROW = re.compile(
    r'<tr>\s*<th>(.*?)</th>\s*<td>(.*?)</td>\s*</tr>', re.I | re.S)


def _text(fragment: str) -> str:
    """Strip tags and collapse whitespace, preserving nothing but the words."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _first_line(cell: str) -> str:
    """The name out of a NAME<br/>street<br/>city cell."""
    return _text(re.split(r"<br\s*/?>", cell, maxsplit=1)[0])


def _wv_parse_search(page: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for org_id, label in _WV_SEARCH_ROW.findall(page):
        name = _text(label)
        if name and (org_id, name) not in out:
            out.append((org_id, name))
    return out


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
        out.append((role_t, name_t))
    return out


REGISTRIES: dict[str, Registry] = {
    "WV": Registry(
        state="WV",
        search_url=("https://apps.sos.wv.gov/business/corporations/"
                    "?SearchType=Name&Search={query}"),
        record_url=("https://apps.sos.wv.gov/business/corporations/"
                    "organization.aspx?org={org_id}"),
        parse_search=_wv_parse_search,
        parse_record=_wv_parse_record,
    ),
}


# --- reading state and organisation out of free-text context -----------------
_STATE_NAMES = {
    "west virginia": "WV", "virginia": "VA", "ohio": "OH", "kentucky": "KY",
    "pennsylvania": "PA", "maryland": "MD", "california": "CA", "new york": "NY",
    "texas": "TX", "florida": "FL", "delaware": "DE", "illinois": "IL",
    "massachusetts": "MA", "washington": "WA", "georgia": "GA", "michigan": "MI",
    "north carolina": "NC", "colorado": "CO", "tennessee": "TN", "arizona": "AZ",
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
def _get(client: httpx.Client, url: str) -> str:
    r = client.get(url, timeout=_TIMEOUT, follow_redirects=True,
                   headers={"User-Agent": _UA})
    r.raise_for_status()
    return r.text


def officers_for_org(org_name: str, state: str, *,
                     client: httpx.Client | None = None) -> list[Officer]:
    """Every person named in an official capacity on filings matching `org_name`."""
    reg = REGISTRIES.get((state or "").upper())
    if not reg or not org_name:
        return []

    own = client or httpx.Client()
    try:
        try:
            page = _get(own, reg.search_url.format(query=httpx.QueryParams(
                {"q": org_name})["q"].replace(" ", "+")))
        except Exception as exc:                     # network, 4xx/5xx, timeout
            log.debug("registry search failed for %r in %s: %s", org_name, state, exc)
            return []

        found: list[Officer] = []
        for org_id, entity_name in reg.parse_search(page)[:_MAX_ENTITIES_PER_ORG]:
            url = reg.record_url.format(org_id=org_id)
            try:
                record = _get(own, url)
            except Exception as exc:
                log.debug("registry record %s failed: %s", org_id, exc)
                continue
            for role, name in reg.parse_record(record):
                found.append(Officer(name=name, role=role, org_name=entity_name,
                                     org_id=org_id, state=reg.state, source_url=url))
        return found
    finally:
        if client is None:
            own.close()


def co_officers(person: str, context: str, *,
                client: httpx.Client | None = None) -> list[dict[str, str]]:
    """Registry-attested people who share a filing with `person`.

    `context` is the endpoint's context string — it supplies both the state and
    the organisations to look up. Returns plain dicts because this rides into the
    model payload as JSON. The endpoint themselves is filtered out: the useful
    output is who ELSE is on the filing.
    """
    state = state_from_text(context)
    if not state:
        return []

    person_key = _norm(person)
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    own = client or httpx.Client()
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
            for off in officers_for_org(org, state, client=own):
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
        if client is None:
            own.close()
    return out


def _norm(name: str) -> str:
    """Compare names ignoring case, punctuation and honorifics."""
    n = re.sub(r"[^a-z ]", " ", (name or "").lower())
    n = re.sub(r"\b(dr|mr|mrs|ms|jr|sr|ii|iii|iv)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()
