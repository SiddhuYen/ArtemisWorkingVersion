"""Server-side verification of every `source_url`.

Fetch the page, confirm it resolves, and confirm it names both people in the hop.
A hop whose citation cannot be shown to mention both endpoints is not a sourced
hop, whatever the model said about it.

Deliberate asymmetry in the default policy:

* Page fetched, real text, a name is absent  -> drop the path. The citation does
  not support the claim.
* Page unreachable, or fetched but unreadable -> downgrade to weak. Absence of
  evidence here is our failure to read the page, not proof the claim is false;
  403s, paywalls, PDFs and JS shells are all common on exactly the primary
  sources the prompt asks for.

The `min_text_chars` floor is what keeps that distinction honest — a page that
returns 200 with 90 characters of markup is classed unreadable, not contradicted.
"""

from __future__ import annotations

import html
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable
from urllib.parse import urlsplit

import httpx

from .config import Action, VerifyConfig, Verdict

_SCRIPT_STYLE = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)

# Dropped when tokenising a person's name.
_PARTICLES = {
    "de", "del", "der", "di", "da", "du", "la", "le", "van", "von", "bin",
    "ibn", "al", "st", "jr", "sr", "ii", "iii", "iv", "dr", "mr", "ms", "mrs",
    "prof", "sir", "the",
}


@dataclass
class UrlCheck:
    url: str
    verdict: Verdict
    action: Action
    status: int | None = None
    final_url: str | None = None
    content_type: str | None = None
    text_chars: int = 0
    names_expected: tuple[str, str] = ("", "")
    names_found: dict[str, bool] = field(default_factory=dict)
    error: str | None = None
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["names_expected"] = list(self.names_expected)
        return d


# --------------------------------------------------------------------------- #
# name matching
# --------------------------------------------------------------------------- #

def normalize(text: str) -> str:
    """Casefold, strip accents, drop punctuation, collapse whitespace."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    lowered = stripped.casefold()
    return _WS.sub(" ", _NON_WORD.sub(" ", lowered)).strip()


def name_tokens(name: str) -> list[str]:
    """Tokenise a person's name, discarding any trailing role/org qualifier."""
    head = re.split(r",| at | of | \(| — | - ", name, maxsplit=1)[0]
    return [t for t in normalize(head).split() if len(t) > 1 and t not in _PARTICLES]


# Short forms that are not prefixes of the formal name, so no amount of string
# comparison finds them. Not exhaustive — it does not need to be, because the
# prefix rule below already covers the large majority (ari/ariel, dan/daniel,
# jon/jonathan, chris/christopher).
_NICKNAMES = {
    "bob": "robert", "rob": "robert", "bobby": "robert",
    "bill": "william", "billy": "william", "will": "william",
    "dick": "richard", "rick": "richard",
    "jim": "james", "jimmy": "james",
    "jack": "john", "johnny": "john",
    "peggy": "margaret", "meg": "margaret",
    "betty": "elizabeth", "liz": "elizabeth", "beth": "elizabeth",
    "kate": "katherine", "katie": "katherine", "kathy": "katherine",
    "tony": "anthony", "ted": "edward", "ned": "edward",
    "hank": "henry", "harry": "henry", "chuck": "charles",
    "sandy": "alexander", "nancy": "ann", "sally": "sarah",
}


def _canon_given(tok: str) -> str:
    return _NICKNAMES.get(tok, tok)


def _given_matches(given: str, found: str) -> bool:
    """Would `found` (a token sitting immediately before the surname) be this person?"""
    if given == found:
        return True
    if _canon_given(given) == _canon_given(found):
        return True
    # Formal vs. common form: 'ari' / 'ariel', 'jon' / 'jonathan'. Three
    # characters minimum so 'jo' does not match 'joseph' and 'john' alike.
    short, long_ = sorted((given, found), key=len)
    if len(short) >= 3 and long_.startswith(short):
        return True
    # Initial: 'a emanuel' (punctuation is already stripped by normalize()).
    return len(found) == 1 and found == given[0]


def name_present(name: str, haystack_norm: str) -> bool:
    """True if `haystack_norm` plausibly names this person.

    The surname must appear, and the token immediately before it must be a form
    of the given name. Matching on the ADJACENT token rather than the given name
    appearing anywhere on the page is what keeps this from firing on a document
    that happens to contain both 'Ari' and 'Emanuel' in unrelated places.

    Given-name forms are matched loosely — exact, nickname, prefix, or initial —
    because primary sources use legal names. A TKO proxy statement says 'Ariel
    Emanuel' 28 times and 'Ari Emanuel' never; requiring an exact token there
    rejected the filing outright. SEC filings, proxies and board minutes are
    exactly the sources this system prefers, so the strict rule failed hardest
    where it was supposed to be strongest.

    Surnames are still matched exactly. That asymmetry is deliberate: given
    names have well-known variant forms, surnames mostly do not, and loosening
    both would start confirming hops between different people.
    """
    tokens = name_tokens(name)
    if not tokens:
        return False

    padded = f" {haystack_norm} "

    if len(tokens) == 1:
        return f" {tokens[0]} " in padded

    given, surname = tokens[0], tokens[-1]

    # Exact full name, including any middle tokens.
    if f" {' '.join(tokens)} " in padded:
        return True

    sur = re.escape(surname)
    # 'ariel emanuel', 'a emanuel'
    for m in re.finditer(rf"\b(\w+)\s+{sur}\b", haystack_norm):
        if _given_matches(given, m.group(1)):
            return True
    # 'ariel z emanuel' — a middle initial or middle name between the two.
    for m in re.finditer(rf"\b(\w+)\s+\w{{1,12}}\s+{sur}\b", haystack_norm):
        if _given_matches(given, m.group(1)):
            return True
    return False


def html_to_text(body: str) -> str:
    without_scripts = _SCRIPT_STYLE.sub(" ", body)
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", without_scripts))).strip()


# --------------------------------------------------------------------------- #
# fetching
# --------------------------------------------------------------------------- #

def _is_soft_domain(url: str, soft_domains: Iterable[str]) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(d in host for d in soft_domains)


def _fetch(client: httpx.Client, url: str, cfg: VerifyConfig) -> tuple[str, dict[str, Any]]:
    """Return (extracted_text, metadata). Raises httpx errors to the caller."""
    meta: dict[str, Any] = {"status": None, "final_url": None, "content_type": None}
    encoding = "utf-8"
    with client.stream("GET", url) as resp:
        meta["status"] = resp.status_code
        meta["final_url"] = str(resp.url)
        meta["content_type"] = resp.headers.get("content-type", "").split(";")[0].strip() or None
        encoding = resp.encoding or "utf-8"
        if resp.status_code >= 400:
            return "", meta

        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_bytes():
            chunks.append(chunk)
            total += len(chunk)
            if total >= cfg.max_bytes:
                break
        raw = b"".join(chunks)

    ctype = meta["content_type"] or ""
    if ctype and not (ctype.startswith("text/") or "html" in ctype or "xml" in ctype or "json" in ctype):
        # PDFs and office docs are common primary sources but need a parser we
        # deliberately do not depend on. Report as unreadable, not contradicted.
        return "", meta

    body = raw.decode(encoding, errors="replace")
    return html_to_text(body) if ("html" in ctype or "xml" in ctype or not ctype) else body, meta


def check_url(
    client: httpx.Client,
    url: str,
    name_a: str,
    name_b: str,
    cfg: VerifyConfig,
) -> UrlCheck:
    check = UrlCheck(url=url, verdict="unreachable", action="downgrade", names_expected=(name_a, name_b))

    if not url or not url.lower().startswith(("http://", "https://")):
        check.error = "not an http(s) URL"
        check.verdict = "unreachable"
        check.action = cfg.actions["unreachable"]
        return check

    # Classify aggregators before fetching. An aggregator profile is
    # insufficient on its own whether or not we can read it, and these hosts
    # bot-block anyway (LinkedIn answers with HTTP 999) — fetching would
    # mislabel a soft source as merely unreachable.
    if _is_soft_domain(url, cfg.soft_domains):
        check.verdict = "soft_source"
        check.action = cfg.actions["soft_source"]
        check.note = "aggregator/contact-database domain — not sufficient on its own"
        return check

    try:
        text, meta = _fetch(client, url, cfg)
    except httpx.HTTPError as exc:
        check.error = f"{type(exc).__name__}: {exc}"
        check.action = cfg.actions["unreachable"]
        return check

    check.status = meta["status"]
    check.final_url = meta["final_url"]
    check.content_type = meta["content_type"]
    check.text_chars = len(text)

    if check.status is None or check.status >= 400:
        check.error = f"HTTP {check.status}"
        check.verdict = "unreachable"
        check.action = cfg.actions["unreachable"]
        if check.status in (401, 402, 403, 429, 451, 999):
            check.note = (
                f"fetch blocked (HTTP {check.status}) — the claim is unverified, not "
                "disproved. If this recurs on primary sources, check VerifyConfig."
                "user_agent: sec.gov and wikipedia.org reject spoofed browser agents."
            )
        return check

    # Redirected into an aggregator after starting somewhere else.
    if _is_soft_domain(check.final_url or url, cfg.soft_domains):
        check.verdict = "soft_source"
        check.action = cfg.actions["soft_source"]
        check.note = "redirects to an aggregator/contact-database domain — not sufficient on its own"
        norm = normalize(text)
        check.names_found = {name_a: name_present(name_a, norm), name_b: name_present(name_b, norm)}
        return check

    if check.text_chars < cfg.min_text_chars:
        check.verdict = "unverifiable"
        check.action = cfg.actions["unverifiable"]
        check.note = (
            f"only {check.text_chars} chars of extractable text "
            f"(< {cfg.min_text_chars}) — likely PDF, paywall, or JS-rendered"
        )
        return check

    norm = normalize(text)
    found_a = name_present(name_a, norm)
    found_b = name_present(name_b, norm)
    check.names_found = {name_a: found_a, name_b: found_b}

    if found_a and found_b:
        check.verdict = "verified"
        check.action = cfg.actions["verified"]
    else:
        missing = [n for n, ok in ((name_a, found_a), (name_b, found_b)) if not ok]
        check.verdict = "names_missing"
        check.action = cfg.actions["names_missing"]
        check.note = f"page does not name: {', '.join(missing)}"
    return check


def verify_hops(
    hops: list[dict[str, Any]],
    cfg: VerifyConfig,
    on_progress: Any = None,
) -> list[UrlCheck]:
    """Check every hop's source_url concurrently. Returns one check per hop, in order."""
    if not hops:
        return []

    headers = {
        "User-Agent": cfg.user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    results: list[UrlCheck | None] = [None] * len(hops)

    with httpx.Client(
        follow_redirects=True,
        timeout=cfg.timeout_s,
        headers=headers,
    ) as client:
        def run(i: int) -> tuple[int, UrlCheck]:
            hop = hops[i]
            return i, check_url(
                client,
                str(hop.get("source_url", "")),
                str(hop.get("from", "")),
                str(hop.get("to", "")),
                cfg,
            )

        workers = max(1, min(cfg.max_concurrency, len(hops)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for i, check in pool.map(run, range(len(hops))):
                results[i] = check
                if on_progress:
                    host = urlsplit(check.final_url or check.url).hostname or check.url
                    detail = check.note or check.error or ""
                    on_progress(
                        f"source {check.verdict}: {host}"
                        + (f" — {detail}" if detail else "")
                    )

    return [c for c in results if c is not None]
