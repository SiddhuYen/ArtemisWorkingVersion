"""Route finding, powered by the warm_intro wrapper.

This replaces the graph/providers/extraction engine that used to answer
/connect. The two work very differently and the difference is visible to the
operator, so it is worth stating plainly:

  old: expand both people's networks with a search API, meet in the middle, and
       return every path found — many routes, ranked, of varying quality.
  new: research the public professional record for ONE well-sourced route, gated
       on whether each endpoint is in that record at all. At most one route, and
       it may legitimately come back empty.

So this endpoint now returns 0 or 1 routes where it used to return several. That
is the intended trade: a single sourced route is worth more than five plausible
ones when the next step is a real person spending real social capital.

When there is no route, `warm_intro.verdict` says which kind of nothing it is —
NEED_DISAMBIGUATION is fixed by adding a role to the context box, NO_PATH is not,
and TARGET_MAP_ONLY carries named people close to the target in
`warm_intro.approach_surface` even though nothing connects to them yet.

The wire contract to the UI is unchanged — {connected, paths[], reason} with
path nodes carrying label / relationship_from_previous / via_orgs / evidence /
confidence / source_url — so app.js renders and merges results exactly as before.
"""

from __future__ import annotations

from typing import Any, Callable

from warm_intro import PathfinderConfig, RefusalError, find_path
from warm_intro.client import PathfinderError
from warm_intro.neighbors import find_neighbors

from . import contacts

# One budget, no dial. The depth selector is gone: it asked the operator to
# guess how hard a search would be before it started, which is not knowable up
# front, and a wrong guess showed up as "no route found" rather than as "you
# picked too low". The model already stops as soon as a route passes its hop
# tests — a typical run finishes in 2-5 searches — so this is a ceiling on the
# hard cases, not a target every run spends.
MAX_SEARCHES = 20

# How many of the starting person's connections travel in the prompt. Ten is
# enough to give the model real choices without turning the payload into a
# directory — and the payload sits after the cache breakpoint, so it costs full
# input price on every run.
NETWORK_SHORTLIST = 10

# The tool block renders at position 0 of the prompt, ahead of the system
# prompt, so this stays byte-identical across requests and the cached prefix
# survives between runs. With a single budget the two are now the same number,
# which also makes the cap real rather than advisory.
SEARCH_CEILING = MAX_SEARCHES

_STRENGTH_CONFIDENCE = {"strong": 0.9, "moderate": 0.65, "weak": 0.35}
_RATING_SCORE = {"strong": 90, "moderate": 65, "weak": 35, "dropped": 0}


def _qualify(name: str, context: str) -> str:
    """Attach the operator's disambiguating context to a bare name.

    'Dana White' finds the wrong person; 'Dana White, CEO of TKO' does not. The
    board already collects this as the optional context field, so use it rather
    than sending a bare name and hoping.
    """
    name = (name or "").strip()
    context = (context or "").strip()
    if not context:
        return name
    if context.lower() in name.lower():
        return name
    return f"{name}, {context}"


def _edge_label(hop: dict[str, Any]) -> str:
    """The short tie name that goes on the connection line.

    The board draws this as a pill sized from the character count, so a sentence
    here renders as a bar wider than the graph. The model is asked for a 2-4
    word `relationship` for exactly this; the fallback clips an older or
    non-conforming `basis` at a word boundary rather than trusting it.

    Nothing is lost by clipping: the full sentence still travels as `evidence`
    and is what the hover card shows.
    """
    rel = (hop.get("relationship") or "").strip()
    if rel:
        return rel if len(rel) <= _MAX_EDGE_LABEL else rel[:_MAX_EDGE_LABEL].rstrip() + "…"
    basis = (hop.get("basis") or "").strip()
    if len(basis) <= _MAX_EDGE_LABEL:
        return basis
    clipped = basis[:_MAX_EDGE_LABEL].rsplit(" ", 1)[0].rstrip(" ,;:—-")
    return (clipped or basis[:_MAX_EDGE_LABEL].rstrip()) + "…"


_MAX_EDGE_LABEL = 34


def _to_ui_path(data: dict[str, Any]) -> list[dict[str, Any]]:
    """wrapper path+hops -> the node list app.js renders.

    The wrapper describes edges separately from nodes; the UI wants each node to
    carry the edge that arrives at it. Node i takes hop i-1; the first node has
    no incoming hop and so carries empty edge fields.
    """
    path = data.get("path") or []
    hops = data.get("hops") or []
    out: list[dict[str, Any]] = []
    for i, node in enumerate(path):
        hop = hops[i - 1] if 0 < i <= len(hops) else None
        org = (node.get("org") or "").strip()
        entry: dict[str, Any] = {
            "label": node.get("name", ""),
            # Short tie name for the line pill; the full sentence rides in
            # `evidence` below and is what the hover card shows.
            "relationship_from_previous": _edge_label(hop) if hop else "",
            "via_orgs": [org] if org else [],
            "evidence": (hop or {}).get("basis", "") if hop else "",
            "confidence": _STRENGTH_CONFIDENCE.get((hop or {}).get("strength", ""), None)
            if hop
            else None,
            "source_url": (hop or {}).get("source_url", "") if hop else "",
            "source_title": (hop or {}).get("source_type", "") if hop else "",
            "method": (hop or {}).get("source_type", "") if hop else "",
            "homonym_flag": None,
            # Not part of the old contract; harmless extra the detail rail can
            # show and the renderer ignores.
            "role": node.get("role", ""),
            "evidence_date": (hop or {}).get("evidence_date", "") if hop else "",
        }
        out.append(entry)
    return out


# What each non-PATH_FOUND verdict means to someone looking at the board. The
# distinction is worth keeping: NEED_DISAMBIGUATION is fixed by typing a role
# into the context box, NO_PATH is not fixed by anything the operator can type,
# and TARGET_MAP_ONLY means we mapped the target but have nothing to route from.
_VERDICT_PREFIX = {
    "NEED_DISAMBIGUATION": "need a qualifier",
    "TARGET_MAP_ONLY": "mapped the target, but no route in",
    "INVALID_REQUEST": "invalid request",
}


def _reason(data: dict[str, Any]) -> str:
    """Why there is no route, in the operator's words rather than a status code."""
    weak = [w for w in (data.get("weak_points") or []) if isinstance(w, str)]
    detail = ""
    if weak:
        detail = weak[0]
    else:
        considered = data.get("clusters_considered") or []
        if considered and isinstance(considered[0], dict):
            detail = str(considered[0].get("why_dropped") or "")
    if not detail:
        detail = "no hop could be sourced to a named, public professional connection"

    prefix = _VERDICT_PREFIX.get(str(data.get("verdict")))
    return f"{prefix} — {detail}" if prefix else detail


def _is_operator(person: str, operator_name: str) -> bool:
    """Is the route starting from the person whose contacts we hold?

    Compared on normalised names. The contact list is only evidence about the
    person who uploaded it, so routing through it when the origin is someone
    else would assert relationships that nobody has claimed.
    """
    if not operator_name:
        return False
    return contacts.norm_name(person) == contacts.norm_name(operator_name)


def run_route(
    person_a: str,
    person_b: str,
    ask: str = "N/A",
    context_a: str = "",
    context_b: str = "",
    on_progress: Callable[[str], None] | None = None,
    db: Any = None,
    operator_name: str = "",
) -> dict[str, Any]:
    """Find a verified route from person_a to person_b. Returns the UI contract.

    When `person_a` is the operator and their contacts have been imported, the
    most relevant connections are ranked locally and travel in the payload, and
    the model may use one for the first hop without a public source. Anyone else
    as the origin gets no list at all — not an empty one — so there is nothing to
    mistakenly route through.
    """
    max_searches = MAX_SEARCHES

    cfg = PathfinderConfig(
        search_ceiling=SEARCH_CEILING,
        progress_sink=on_progress,
    )

    # The shortlist is chosen here, not by the model. Ranking a few thousand
    # rows is a local job, and doing it up front keeps the whole run to a single
    # request: no tool round trips, each of which re-sent the entire
    # conversation just to hand back a database lookup.
    shortlist: list[dict[str, Any]] = []
    if db is not None and _is_operator(person_a, operator_name):
        held = contacts.count_for_owner(db, operator_name)
        if held:
            shortlist = contacts.top_candidates(
                db, operator_name, _qualify(person_b, context_b),
                limit=NETWORK_SHORTLIST)
            if on_progress:
                on_progress(
                    f"routing from {operator_name} — {len(shortlist)} of {held} "
                    "connection(s) shortlisted as possible first hops")

    payload: dict[str, Any] = {
        "target": _qualify(person_b, context_b),
        "starting_person": _qualify(person_a, context_a),
        "ask": (ask or "").strip() or "N/A",
        "max_searches": max_searches,
    }
    if shortlist:
        payload["starting_person_connections"] = shortlist

    try:
        result = find_path(payload, config=cfg)
    except RefusalError as exc:
        # A refusal is a real outcome, not a crash: surface it as "no route" with
        # the reason rather than a 500 the operator cannot act on.
        return {
            "connected": False,
            "reason": f"the request was declined by safety classifiers ({exc.category})",
            "paths": [],
            "warm_intro": {"refusal": True, "category": exc.category},
        }
    except PathfinderError as exc:
        return {
            "connected": False,
            "reason": str(exc),
            "paths": [],
            "warm_intro": {"error": type(exc).__name__},
        }

    data = result.data
    diagnostics = {
        "verdict": data.get("verdict"),
        "rating": data.get("rating"),
        "weak_points": data.get("weak_points") or [],
        # Named people close to the target, returned when there was nothing to
        # route from. Empty for every other verdict.
        "approach_surface": data.get("approach_surface") or [],
        "first_action": data.get("first_action") or {},
        "clusters_considered": data.get("clusters_considered") or [],
        "target_context": data.get("target_context", ""),
        "searches_used": data.get("searches_used"),
        "usage": result.usage,
        "validation": result.validation,
    }

    if not data.get("path"):
        return {
            "connected": False,
            "reason": _reason(data),
            "paths": [],
            "warm_intro": diagnostics,
        }

    ui_path = _to_ui_path(data)
    return {
        "connected": True,
        "paths": [
            {
                "path": ui_path,
                "hops": max(0, len(ui_path) - 1),
                "score": _RATING_SCORE.get(str(data.get("rating")), 0),
            }
        ],
        # Legacy single-path fields, for any caller reading the pre-`paths` shape.
        "path": ui_path,
        "hops": max(0, len(ui_path) - 1),
        "score": _RATING_SCORE.get(str(data.get("rating")), 0),
        "warm_intro": diagnostics,
    }


def run_discover(
    person_name: str,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Map one person's closest verified professional connections.

    Answers /discover. `hops` is always 1 here: these are direct connections to
    the person, not a multi-hop expansion — the old engine's depth-N crawl has
    no equivalent, and reporting a fake hop count would be worse than reporting
    the true one.
    """
    cfg = PathfinderConfig(search_ceiling=SEARCH_CEILING, progress_sink=on_progress)

    try:
        result = find_neighbors(person_name, max_searches=MAX_SEARCHES, config=cfg)
    except RefusalError as exc:
        return {"found": False,
                "reason": f"the request was declined by safety classifiers ({exc.category})"}
    except PathfinderError as exc:
        return {"found": False, "reason": str(exc)}

    data = result.data
    conns = data.get("connections") or []
    if not conns:
        notes = data.get("notes") or []
        return {
            "found": False,
            "reason": notes[0] if notes else
                      f"no connection to {person_name} could be sourced to a named public record",
        }

    return {
        "found": True,
        "person": data.get("person") or person_name,
        "count": len(conns),
        "connections": [
            {
                "label": c.get("name", ""),
                "relationship_type": c.get("relationship_type") or c.get("basis", ""),
                "confidence": _STRENGTH_CONFIDENCE.get(c.get("strength", ""), 0.35),
                "hops": 1,
                "source_url": c.get("source_url", ""),
                "role": c.get("role", ""),
                "org": c.get("org", ""),
                "evidence": c.get("basis", ""),
                "evidence_date": c.get("evidence_date", ""),
            }
            for c in conns
        ],
        "warm_intro": {
            "person_context": data.get("person_context", ""),
            "notes": data.get("notes") or [],
            "searches_used": data.get("searches_used"),
            "usage": result.usage,
        },
    }
