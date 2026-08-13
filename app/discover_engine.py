"""Discover: open-ended people search, powered by the warm_intro wrapper.

Routing answers "can I get to this person". Discover answers the question that
comes before it — "who should that person even be?" — from a population in the
operator's own words: `superintendents`, `software engineers who work on
compilers`, `family-office CIOs in the Southeast`.

It returns the strongest members of that population that the operator can
actually reach, each with the artifact that makes them exceptional and the named
person who bridges to them. Both halves matter and neither is sufficient: rank on
excellence alone and you get the list a magazine would print, none of whom take
the call; rank on reachability alone and you get the address book back.

WHAT A REACH CLAIM IS WORTH HERE. Less than a route, deliberately. One run
sizes reach for a whole population on the budget a single route spends on one
pair, so a `bridged` candidate carries one sourced sentence rather than a
verified chain. That is why every candidate ships with the context needed to hand
it straight to `/connect`, which is where a lead becomes a chain. Discover
proposes; route verifies.

Unreachable candidates are returned too, marked `cold`, rather than filtered out.
The operator is regularly the only person who knows about a tie that exists
nowhere in the public record, and a name they recognise is the most valuable
thing this can hand them.

Owner scoping matches /connect exactly: the imported connections travel in the
payload only when the origin IS the authenticated operator, resolved from the
session and never from the request. Discover from anyone else and the key is
omitted entirely — not sent empty — so there is nothing to accidentally search
through. Those are not their connections.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from warm_intro import PathfinderConfig, RefusalError
from warm_intro.client import PathfinderError
from warm_intro.discovery import SOURCED_REACH, find_people

from . import contacts, registry

log = logging.getLogger(__name__)

# Discover pays for two passes where a route pays for one: it maps a population
# AND tests reach into it, per candidate. 20 searches — the route budget — spends
# out while the second pass is still running, which surfaces as a list of famous
# names with `cold` reach, i.e. the exact failure the feature exists to avoid.
# This is a ceiling on the hard cases, not a target: the model stops as soon as
# it has a sourced set.
MAX_SEARCHES = 30

# The tool block renders at position 0 of the prompt, ahead of the system
# prompt, so this stays byte-identical across requests and the cached prefix
# survives between runs. Same number as MAX_SEARCHES, which makes the cap real
# rather than advisory.
SEARCH_CEILING = MAX_SEARCHES

# How many of the operator's connections travel in the payload. Four times the
# route shortlist, because a route is aimed at one named person while this is
# aimed at a whole population — and the list has to carry both the connections
# who ARE in that population and the ones who merely bridge to it. The payload
# sits after the cache breakpoint, so it costs full input price on every run;
# 40 rows is roughly 700 tokens, which is cheap against a 30-search turn.
NETWORK_SHORTLIST = 40

# How many candidates to ask for. A ceiling the model is told never to pad to.
DEFAULT_LIMIT = 10
MAX_LIMIT = 25

_STRENGTH_CONFIDENCE = {"strong": 0.9, "moderate": 0.65, "weak": 0.35}
_STANDOUT_SCORE = {"exceptional": 95, "strong": 75, "notable": 55}

# What each reach status means to someone looking at a card. The distinction is
# the whole point of the field: `direct` is actionable today, `bridged` needs one
# email, `surface_only` needs the operator to know something we do not, and
# `cold` is a name they may recognise and we cannot route to.
_REACH_LABEL = {
    "direct": "you already know them",
    "bridged": "one intro away",
    "surface_only": "nobody found who reaches them",
    "cold": "no route in",
}


def _qualify(name: str, context: str) -> str:
    """Attach the operator's disambiguating context to a bare name.

    Same job as the router's: 'Dana White' finds the wrong person, 'Dana White,
    CEO of TKO' does not. Here it qualifies the ORIGIN, whose surface decides
    both what scope the population is bounded to and what counts as reach.
    """
    name = (name or "").strip()
    context = (context or "").strip()
    if not context:
        return name
    if context.lower() in name.lower():
        return name
    return f"{name}, {context}"


def _is_operator(person: str, operator_name: str) -> bool:
    """Is the origin the person whose contacts we hold?

    Compared on normalised names. The contact list is only evidence about the
    person who uploaded it, so searching it on behalf of someone else would
    assert relationships nobody has claimed. `operator_name` is the display name
    from the resolved identity, never a request field.
    """
    if not operator_name:
        return False
    return contacts.norm_name(person) == contacts.norm_name(operator_name)


def _to_ui_candidate(cand: dict[str, Any]) -> dict[str, Any]:
    """One wrapper candidate -> the card the UI renders.

    `context` is assembled here rather than in the browser because it is what the
    per-candidate route button posts as `context_b`: /connect rejects a bare name,
    correctly, since one resolves to the wrong person and then cites real sources
    about somebody else. Discover already knows the role and org, so the hand-off
    carries them instead of asking the operator to retype what is on the card.
    """
    reach = cand.get("reach") or {}
    role = cand.get("role", "")
    org = cand.get("org", "")
    evidence = cand.get("evidence") or []
    context = ", ".join(p for p in (role, org) if p) or cand.get("locator", "")

    return {
        "label": cand.get("name", ""),
        "role": role,
        "org": org,
        "locator": cand.get("locator", ""),
        "context": context,
        "why_cracked": cand.get("why_cracked", ""),
        "standout": cand.get("standout", "notable"),
        "score": _STANDOUT_SCORE.get(cand.get("standout", ""), 55),
        "evidence": evidence,
        # The card is clickable through to the first named object; the rest are
        # listed on the detail rail.
        "source_url": next((e.get("source_url") for e in evidence
                            if e.get("source_url")), ""),
        "opening": cand.get("opening", ""),
        "reach": {
            "status": reach.get("status", "cold"),
            "label": _REACH_LABEL.get(reach.get("status", ""), "no route in"),
            "via": reach.get("via", ""),
            "via_locator": reach.get("via_locator", ""),
            "basis": reach.get("basis", ""),
            "source_url": reach.get("source_url", ""),
            "source_type": reach.get("source_type", ""),
            "evidence_date": reach.get("evidence_date", ""),
            "hops": reach.get("hops", 0),
            "strength": reach.get("strength", ""),
            "confidence": _STRENGTH_CONFIDENCE.get(reach.get("strength", ""), None),
        },
    }


def _reason(data: dict[str, Any], prompt: str) -> str:
    """Why the list is empty, in the operator's words rather than a status code."""
    notes = [n for n in (data.get("notes") or []) if isinstance(n, str) and n.strip()]
    if notes:
        return notes[0]
    return (f"nobody in {prompt!r} could be tied to a named artifact in the "
            f"public record")


def run_discover(
    prompt: str,
    origin_name: str = "",
    ask: str = "N/A",
    origin_context: str = "",
    limit: int = DEFAULT_LIMIT,
    on_progress: Callable[[str], None] | None = None,
    db: Any = None,
    operator_id: str = "",
    operator_name: str = "",
) -> dict[str, Any]:
    """Find the most exceptional members of `prompt` reachable from the origin.

    When `origin_name` is the operator and their contacts have been imported, the
    connections most relevant to the population are ranked locally and travel in
    the payload — as candidates the operator already knows, and as bridges into
    the ones they do not. Anyone else as the origin gets no list at all, not an
    empty one.
    """
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    origin = _qualify(origin_name, origin_context)

    cfg = PathfinderConfig(
        search_ceiling=SEARCH_CEILING,
        progress_sink=on_progress,
        # An artifact is confirmed by reading the page, not by reading a search
        # snippet about the page. Without this the model judges "did this person
        # actually do the thing" from a headline, which is how a press release
        # becomes an accomplishment. Fetches are capped by the same max_uses as
        # searches but are not counted against max_searches, so this raises
        # per-run cost.
        enable_web_fetch=True,
    )

    # The shortlist is chosen here, not by the model. Ranking a few thousand rows
    # is a local job, and doing it up front keeps the whole run to a single
    # request rather than a tool round trip that re-sends the conversation just
    # to hand back a database lookup.
    shortlist: list[dict[str, Any]] = []
    if db is not None and operator_id and origin_name and _is_operator(origin_name, operator_name):
        held = contacts.count_for_owner(db, operator_id)
        if held:
            shortlist = contacts.top_by_query(db, operator_id, prompt,
                                              limit=NETWORK_SHORTLIST)
            if on_progress:
                on_progress(
                    f"searching from {operator_name} — {len(shortlist)} of {held} "
                    "connection(s) shortlisted as candidates or bridges")

    # Records silo: co-officers from state business filings. The filing that
    # names two people together is often the only hard tie a regional operator
    # has, and it is invisible to search — it sits behind a query form under a
    # numeric id. Best-effort: a registry that is down costs nothing, and a state
    # with no entry yields nothing.
    co_officers: list[dict[str, str]] = []
    if origin_name:
        try:
            co_officers = registry.co_officers(origin_name, origin_context or "")
        except Exception as exc:               # never fail a build over enrichment
            log.warning("registry lookup failed for %r: %s", origin_name, exc)
            co_officers = []
        if co_officers and on_progress:
            names = ", ".join(f["name"].title() for f in co_officers[:4])
            on_progress(f"state business filings name {len(co_officers)} "
                        f"co-officer(s) with {origin_name}: {names}")

    try:
        result = find_people(
            prompt,
            origin=origin,
            ask=ask,
            limit=limit,
            max_searches=MAX_SEARCHES,
            origin_connections=shortlist or None,
            origin_co_officers=co_officers or None,
            config=cfg,
        )
    except RefusalError as exc:
        # A refusal is a real outcome, not a crash: surface it as "nothing found"
        # with the reason rather than a 500 the operator cannot act on.
        return {
            "found": False,
            "reason": f"the request was declined by safety classifiers ({exc.category})",
            "prompt": prompt,
            "warm_intro": {"refusal": True, "category": exc.category},
        }
    except PathfinderError as exc:
        return {
            "found": False,
            "reason": str(exc),
            "prompt": prompt,
            "warm_intro": {"error": type(exc).__name__},
        }

    data = result.data
    diagnostics = {
        "notes": data.get("notes") or [],
        "searches_used": data.get("searches_used"),
        "usage": result.usage,
        "validation": result.validation,
    }

    candidates = [_to_ui_candidate(c) for c in (data.get("candidates") or [])]
    if not candidates:
        return {
            "found": False,
            "reason": _reason(data, prompt),
            "prompt": prompt,
            "origin": origin,
            "interpretation": data.get("interpretation") or {},
            "warm_intro": diagnostics,
        }

    reachable = sum(1 for c in candidates if c["reach"]["status"] in SOURCED_REACH)
    return {
        "found": True,
        "prompt": prompt,
        "origin": origin,
        "brief": data.get("brief", ""),
        "interpretation": data.get("interpretation") or {},
        "origin_surface": data.get("origin_surface") or [],
        "count": len(candidates),
        # Named separately because it is the number that decides whether the run
        # was useful. Ten candidates and zero reachable means the population was
        # mapped and the operator cannot touch any of it — a real result, and a
        # different one from ten candidates with six routes in.
        "reachable_count": reachable,
        "candidates": candidates,
        "warm_intro": diagnostics,
    }
