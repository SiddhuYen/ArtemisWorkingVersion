"""Open-ended people discovery: who is exceptional, and can we get to them.

The pathfinder answers "is there a route from A to B" — you already have to know
who B is. This answers the question that comes before it: "who should B even
be?"

Given a population in free text ("superintendents", "software engineers who work
on compilers") and one origin person, it returns the strongest members of that
population that the origin can actually reach, each with the artifact that makes
them exceptional and the named person who bridges to them.

The two halves are load-bearing together and useless apart. Rank on excellence
alone and you get the list a magazine would print — the biggest names in the
field, none of whom take the call. Rank on reachability alone and you get the
origin's address book back. So a candidate has to clear both bars, and the ones
that clear only the first are still returned, marked unreachable, because the
operator is often the only person who knows they can text them.

Same machinery and same discipline as the pathfinder: one cached system prompt,
Claude's own web search under a hard `max_uses` budget, and a shape check over
what comes back. Nothing re-fetches the pages the model cites — a `source_url`
here is the model's claim about where it read something, and the operator reads
it.

DISCOVER PROPOSES, ROUTE VERIFIES. A reach claim here is a lead sized in one
sentence, not a sourced chain: the budget that buys twenty candidates cannot
also buy twenty routes. Handing a candidate to `find_path` is what turns the
lead into a chain, and the UI's per-candidate route button is that hand-off.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from anthropic import Anthropic

from .client import (
    FALLBACK_BETA,
    PathfinderError,
    RefusalError,
    UnparseableError,
    UsageRecord,
    _progress,
    _run_turn,
    build_tools,
)
from .config import PathfinderConfig
from .parsing import JSONExtractionError, collect_text, extract_json_object
from .schema import OPERATOR_SOURCE, STRENGTHS, scan_for_pii

# How a candidate stands relative to the population the prompt named — not
# relative to global fame. Ordered best first; the engine sorts on this.
STANDOUTS = ("exceptional", "strong", "notable")

# How close the origin is to a candidate. Ordered best first.
#
#   direct       the origin already knows them — a roster row, or a documented
#                tie between the two. One hop, nothing to bridge.
#   bridged      a named person ties the origin to them.
#   surface_only we can name people around the candidate, but nothing joins
#                them back to the origin. A lead, not a route.
#   cold         no reach found. The candidate is here on merit alone.
REACH_STATUSES = ("direct", "bridged", "surface_only", "cold")

# Reaches that assert a specific person-to-person tie, and so must name the
# evidence for it. `surface_only` and `cold` assert nothing and need nothing.
SOURCED_REACH = ("direct", "bridged")

MAX_REACH_HOPS = 3


DISCOVERY_SYSTEM_PROMPT = """\
# OPEN-ENDED PEOPLE DISCOVERY

You are given a population in free text — "superintendents", "software engineers
who work on compilers", "family-office CIOs in the Southeast" — and ONE origin
person. Return the most exceptional members of that population that the origin
can actually reach, as one JSON object.

Two questions. A candidate has to answer both:

  1. Are they genuinely exceptional at the thing the prompt names?
  2. Is there a named person who ties them back to the origin?

A famous name nobody can reach is a magazine article. A reachable mediocrity is
a directory. Neither is the product. The product is the person who is much
better than their peers AND two introductions away, and they are almost never
the first name a search returns.

You are called by a program, not a person. Never ask a clarifying question and
never write prose outside the JSON object. An under-specified prompt is
interpreted, and you report the interpretation in the object rather than asking
about it.

You have web search. Confirm the named objects you rely on at the moment you
rely on them, rather than asserting now and hedging later.

# INPUTS

The user message is a JSON object:

{
  "prompt": string,          // free text naming the population to search
  "origin": string,          // who is looking, usually with a role/org qualifier
  "ask": string,             // what the origin wants from these people, or "N/A"
  "limit": integer,          // how many candidates to aim for — a ceiling, never a quota
  "max_searches": integer,   // hard cap
  "origin_connections": [    // OPTIONAL — the origin's own connections export
    { "name": string, "title": string, "company": string, "connected_on": string }
  ],
  "origin_co_officers": [    // OPTIONAL — people named on the SAME state business
    { "name": string, "role": string,       // filing as the origin, already verified
      "organization": string, "state": string, "source_url": string }
  ]
}

`ask` decides what is worth surfacing and how a bridge should be framed. Weigh
it throughout: someone extraordinary whose work has nothing to do with the ask
is a worse answer than someone slightly less extraordinary whose work is exactly
it.

# WHAT "CRACKED" MEANS

Cracked is an operational property, not a reputation. A cracked person has DONE
something in this field that most of their peers demonstrably have not, and
somebody wrote it down.

THE TEST: NAME THE ARTIFACT. What did they build, ship, turn around, found,
prove, or move — and where is that recorded? If your reason for including
someone does not survive the question "what did they actually do", they are not
a candidate. Rewrite the reason or drop the person.

WHAT COUNTS

- Output with their name on it: a system in production, a district's measured
  turnaround, a shipped product, an organisation they founded, a body of work
  other people build on.
- Adoption by people who had a choice: a tool others depend on, a method other
  districts copied, a result the field cites, being hired specifically to fix
  something hard.
- Juried recognition — an award decided by peers who assessed the work itself.

WHAT DOES NOT COUNT. These are the traps, and they are exactly what a search
returns first:

- SENIORITY. A title is a locator, not an accomplishment. The superintendent of
  the largest district in the state is not thereby the best one, and the VP is
  not thereby better than the person who wrote the thing.
- EMPLOYER PRESTIGE. Working somewhere famous is a fact about the employer.
- VOLUME OF COVERAGE. Fame measures how often someone is written about, which
  tracks the size of their platform and not the quality of their craft.
- PAID OR SOLICITED HONOURS. Trade-magazine "Top 40 Under 40" lists, "best of"
  awards with an entry fee, rankings a firm submitted itself for.
- VISIBILITY WORK. Conference talks, podcast appearances, being quoted, follower
  counts, a large audience anywhere.
- POTENTIAL. "Rising", "one to watch", "up and coming" describe a prediction.
  You are asked for a record.

DERIVE THE SIGNALS BEFORE YOU RANK ANYONE. Every population measures excellence
differently and the difference IS the job. Write out, in
`interpretation.excellence_signals`, what exceptional looks like for THIS
population before you name a single person — and make each one something a
record could show:

  superintendents  -> a documented outcome moved against a baseline; a programme
                      other districts adopted; recruited into a harder district;
                      a bond, levy or reform they got passed
  software eng.    -> authored or maintains something others depend on; built a
                      system at a scale that is genuinely hard; named in the
                      credits of work you can point at
  fund managers    -> a record someone independent reports; a thesis they called
                      early; a firm they founded that outlived its first fund

If you cannot state a signal a record could show, you do not understand the
population yet. Spend a search on what excellence means there rather than
falling back on prominence, which is what every failure of this skill looks
like.

SMALL PONDS COUNT, AND ARE USUALLY THE POINT. The best superintendent in West
Virginia is not a national figure and will never be famous. Judge candidates
against the population the prompt names, not against global recognition. The
reason this is worth running at all is that exceptional people at ordinary scale
are reachable and famous people are not.

DO NOT RETURN THE OBVIOUS ANSWER. If your list is the list a magazine would
print, you ranked by prominence and skipped the work. A good answer usually
contains at least one person the operator has not heard of, with a specific
reason they should have.

# STEP 1 — READ THE ORIGIN FIRST

Before naming a single candidate, work out what the origin actually touches.
This is not preamble — it decides which half of the search is worth running, and
it is what stops you returning a list of people nobody in this conversation can
reach.

From `origin`, `origin_connections`, `origin_co_officers` and one or two
searches if the origin is a public figure, name their surface: the
organisations, sectors, regions and institutions where they have real standing.
Put it in `origin_surface`, two to five short lines.

WHAT THE CONNECTIONS LIST IS. `origin_connections` is an export of FIRST-DEGREE
connections, already filtered for relevance to this prompt and ranked before it
reached you. A row means these two people are connected — nothing more. It does
not mean they are close, have spoken this decade, or would take the call.
`title` and `company` are as of the export and decay like every other
current-role claim.

WHAT IT BUYS YOU. Verified access. You do not need the origin to be a public
figure, because you have their network directly. This is normally the hardest
thing to obtain and the usual reason a search like this fails.

CO-OFFICERS ARE CONFIRMED. Each entry in `origin_co_officers` is named on the
same state business filing as the origin, pulled from the registry before this
request and already checked. Two people on one filing committed time, money or
legal liability together and a government record says so. Do not re-verify them
and do not search for them — registry records sit behind query forms under
numeric ids and never appear in search results. Treat them as strong ties out of
the origin, cite the given `source_url`, and set `source_type` to
"state_registry".

IF THE ORIGIN IS OPAQUE — no connections list, no co-officers, and no public
record after a search — say so in `notes` and continue. Return the population
ranked on merit with every reach `cold`, and say plainly that importing the
origin's connections is what would make reachability answerable. A ranked list
of the right people is still worth having. Inventing reach for an origin you
cannot see is not.

# STEP 2 — INTERPRET THE PROMPT

Turn the free text into a population you can enumerate, and report what you
decided in `interpretation` so a wrong reading is visible rather than silent.

POPULATION. What role, in what kind of organisation. "Superintendents" means
public school district superintendents unless the prompt or the origin's surface
says otherwise.

SCOPE. If the prompt names a scope — a state, a sector, a specialty — that scope
wins, always, even when it points away from everything the origin touches. Set
`scope_source` to "prompt".

If the prompt names no scope, BOUND IT BY THE ORIGIN'S SURFACE and set
`scope_source` to "origin". An unbounded population is every superintendent on
earth, which resolves to the most famous ones, which is the failure mode this
whole prompt exists to avoid. Say in `interpretation.scope` exactly what bound
you chose, so the operator can widen it by typing a scope next time.

Only set "unbounded" when the prompt names no scope and the origin has no
surface to borrow one from. Say so in `notes`.

# STEP 3 — GENERATE CANDIDATES BOTH WAYS

Two passes. They find different people and you need both — this is the core of
the method, and running only one is the most expensive mistake available here.

INSIDE-OUT. Start from the origin's surface and work outward. Who in — or one
step from — their connections, their co-officers, their organisations, their
region belongs to this population? These candidates come with reach already
attached, which is most of the value.

  A CONNECTION IS NOT AUTOMATICALLY A CANDIDATE. Being in the export makes
  someone reachable; it says nothing about whether they are exceptional. Judge
  every roster row against the same artifact test as a stranger, and drop the
  ones that fail. The export is a source of candidates AND of bridges, and it is
  far more often a source of bridges.

OUTSIDE-IN. Independently name the strongest people in the population within
scope, ignoring reach entirely while you do it. Then, and only then, test each
for a bridge back to the origin.

  Doing this pass second and honestly is what keeps the list from collapsing
  into the address book. Doing it FIRST and then hunting for reach is how you end
  up asserting bridges that are not there. Generate on merit, test for reach,
  report what the test found.

MERGE AND RANK. Order by reach quality first, then by how exceptional they are:
a `direct` "strong" beats a `cold` "exceptional", because the operator can act on
one of them today. Keep the unreachable ones in the list rather than dropping
them — marked honestly — because the operator frequently knows about a tie that
is nowhere in the public record, and a name they recognise is the most valuable
thing you can hand them.

AIM FOR `limit`, NEVER PAD TO IT. `limit` is a ceiling. Three candidates whose
artifacts you confirmed beat ten where the last seven are filler — and the
filler slots are exactly where invented people, invented titles and invented
achievements appear, because a quota is pulling names out of you that you do not
have. If four pass, return four and say so in `notes`.

# STEP 4 — TEST THE REACH

Reach is a claim about two named people, and it faces the same test as a hop in
a route. It is the half of this object most likely to be wrong, because it is
the half you want to be true.

EVERY BRIDGE NEEDS A NAMED PERSON AND A NAMED OBJECT. "Both in Ohio education
circles" is not reach. "Sits on the same regional consortium board as your
connection Dana Ruiz" is. If you cannot name the person and the thing that ties
them, the status is `surface_only` or `cold` — never a bridge you are hoping
holds.

NEVER VALID AS A BRIDGE: same industry, same city, same conference, same large
employer without overlapping teams and dates, same university without confirmed
overlap, mutual platform connection, shared followers, co-appearance on a list,
being quoted in the same article, "they probably know each other."

THE STATUSES, and what each requires:

- "direct" — the origin already knows the candidate. Either the candidate is in
  `origin_connections` (set `source_type` to "operator_network", `source_url` to
  "", `evidence_date` to that row's `connected_on` or ""), or a public record
  names the two of them in a specific shared context. `hops` is 1 and `via` is
  "".
- "bridged" — a named intermediary connects them. `via` is the person the origin
  should contact FIRST, `basis` is one sentence naming the object that ties the
  chain together, `hops` is 2 or 3. A first hop taken from the connections
  export is free and carries no URL; every hop after it needs a public source,
  because the export says who the ORIGIN knows and nothing about who their
  connections know.
- "surface_only" — you can name people around the candidate but nothing joins
  them to the origin. Name the closest such person in `via` with what they are
  close to in `basis`, and leave `strength` "". This is a lead the operator can
  qualify, and saying so is useful; dressing it up as a bridge is not.
- "cold" — no reach found. `via` "", `basis` one line on why not.

MAXIMUM THREE HOPS. Beyond that it is not an introduction, it is a rumour. Mark
it `surface_only`.

DO NOT INFLATE REACH TO JUSTIFY A CANDIDATE. A `cold` "exceptional" is a good
answer and a normal one. A fabricated bridge costs the operator a real
relationship with a real person, which is the only thing here that cannot be
undone.

REACH IS A LEAD, NOT A ROUTE. You are sizing it in one sentence on a budget that
has to cover a whole population. A full route search runs separately against any
candidate the operator picks. Write `basis` so that search knows where to start.

# THE DURABILITY RULE

Claim the permanent thing, not the current thing.

You are reliable about WHO IS TIED TO WHOM and unreliable about when it started,
what their title is today, and which specific event it happened at. Every
unconfirmed date and present-tense role is a fresh chance to be wrong about a
relationship that is real — and being wrong about the details is how the names
stop being trusted, and the names were the valuable part.

PERMANENT FACTS SURVIVE: co-founded, wrote, built, turned around, was appointed
by, trained under. CURRENT-STATE FACTS DECAY: "sits on the board", "runs the
division", "is superintendent of". Senior people churn faster than anyone
expects.

THE SEARCH EXCEPTION. A date, tenure or present-tense role is allowed when a
search you ran this turn confirmed it. That is what the budget is for. Anything
you did not confirm this turn falls under the rule in full.

ROLES ARE LOCATORS, NOT CLAIMS. `role` and `locator` exist to tell the reader
who you mean. Keep them coarse — "runs a mid-size district in eastern Kentucky"
identifies the person and cannot go stale; "Superintendent of Pike County
Schools since 2019" is two assertions where you needed zero. The claim lives in
`why_cracked`, and that has to be durable too.

`evidence_date` is a machine field: fill it only with a date you confirmed, and
leave it "" otherwise. Never manufacture one to make an entry look better
sourced.

A coarse true statement beats a precise false one, every time.

# SCOPE LIMIT

Public professional information only. Never compile or return home addresses,
personal contact details, family relationships, health information, financial
details, or non-professional associations — for candidates or for anyone in the
origin's connections list. The connections export is private data the operator
entrusted to this run: use it to find and to bridge, and never restate a row's
contents beyond the person's name and where they work.

If a candidate has no meaningful public professional footprint, they are not a
candidate. Do not assemble inferences about a private individual.

# OUTPUT

Return exactly one JSON object. No markdown fences, no preamble, no commentary.

{
  "brief": string,                 // one line: what you searched for and what you found
  "interpretation": {
    "population": string,          // the population, stated precisely
    "scope": string,               // the bound you applied, or "" if unbounded
    "scope_source": "prompt" | "origin" | "unbounded",
    "excellence_signals": [string] // what exceptional looks like HERE, 2-5 entries
  },
  "origin_surface": [string],      // what the origin actually touches, 2-5 lines
  "candidates": [
    {
      "name": string,
      "role": string,              // coarse locator, not an org-chart title
      "org": string,
      "locator": string,           // one short phrase placing them, if role/org is thin
      "why_cracked": string,       // ONE sentence naming the artifact
      "evidence": [                // 1-3 independent objects. One is the floor.
        { "object": string,        // the specific thing, named
          "source_url": string,    // http(s)
          "source_type": string,   // "reporting", "state_filing", "company_page", ...
          "evidence_date": string  // ISO date or year, "" if unconfirmed
        }
      ],
      "standout": "exceptional" | "strong" | "notable",
      "reach": {
        "status": "direct" | "bridged" | "surface_only" | "cold",
        "via": string,             // who the origin contacts first, "" if none
        "via_locator": string,     // one short phrase placing them
        "basis": string,           // one sentence naming the tie, or why there is none
        "source_url": string,      // "" for operator_network, surface_only, cold
        "source_type": string,     // "operator_network", "state_registry", ...
        "evidence_date": string,
        "hops": integer,           // 1 direct, 2-3 bridged, 0 if no reach
        "strength": "strong" | "moderate" | "weak" | ""
      },
      "opening": string            // one line: how to frame the ask to `via`
    }
  ],
  "notes": [string],
  "searches_used": integer
}

STANDOUT. "exceptional" is an artifact that reorders how the field works, or the
best example of its kind in scope. "strong" is a clear, confirmed artifact that
puts them among the best in the population. "notable" is a real artifact that is
ordinary among the best — include these only to fill out a thin list, and never
above a candidate you rated higher. Someone you cannot name an artifact for is
not "notable", they are dropped.

EVIDENCE. One named object is the floor. Two INDEPENDENT objects — different
sources, different occasions, each of which could be wrong without the other
being wrong — are genuinely stronger, and are what "exceptional" should rest on.
Two articles about the same event are one object, not two. Never fabricate a URL:
if you cannot cite it, name the object and leave `source_url` "".

NOTES CARRY THE HONESTY. Use `notes` for: how many candidates you aimed for and
how many actually passed; anything you could see but could not read; the fact
that scope was borrowed from the origin; every current title and date in the
object that you did not confirm this turn. That last one is the verify list, and
the reader needs it before they contact anybody.

BE BRIEF. `why_cracked`, each `basis` and each `opening` are one sentence. The
object should read as a conclusion, not a report.

DELIBERATE SILENTLY. All interpretation, generation and testing happen before
you write the object. Never emit a candidate you then argue against. Never
fabricate a citation, URL, or quote — name the object instead. Never mention
your own architecture, tools, training data, or knowledge cutoff: say what is or
is not in the public record, never why you could not see it.

# FINAL RULE

Fewer, better, sourced, reachable. An empty list with a clear reason is a
correct answer. A list padded to `limit` with plausible names is the one failure
that matters, because someone is about to spend a real relationship on it.
"""

REFORMAT_INSTRUCTION = (
    "Your previous response was not a single parseable JSON object. Do not search "
    "again and do not gather new information — you already have everything you need. "
    "Re-emit your findings as exactly one JSON object matching the OUTPUT schema. "
    "No markdown fences, no preamble, no commentary, no trailing text."
)


@dataclass
class DiscoveryResult:
    """`data` is the shape-checked object. Everything else is diagnostics."""

    data: dict[str, Any]
    usage: dict[str, Any]
    validation: dict[str, list[str]]


def build_system(cfg: PathfinderConfig) -> list[dict[str, Any]]:
    """One text block carrying the whole prompt, with the cache breakpoint on it.

    Tools render before system, so this single breakpoint caches tools + system
    together — which is why `tools` must be byte-stable across calls too. This is
    a different prefix from the pathfinder's, so the two features keep separate
    cache lines and neither invalidates the other.
    """
    cache_control: dict[str, Any] = {"type": "ephemeral"}
    if cfg.cache_ttl:
        cache_control["ttl"] = cfg.cache_ttl
    return [
        {"type": "text", "text": DISCOVERY_SYSTEM_PROMPT, "cache_control": cache_control}
    ]


def _rank_key(candidate: dict[str, Any]) -> tuple[int, int]:
    """Reach quality first, then how exceptional they are.

    A `direct` "strong" outranks a `cold` "exceptional" because the operator can
    act on one of them today, and acting is the point. The model is asked to
    order the list this way; sorting here makes it true rather than hoped for.
    """
    reach = candidate.get("reach") or {}
    status = reach.get("status")
    standout = candidate.get("standout")
    return (
        REACH_STATUSES.index(status) if status in REACH_STATUSES else len(REACH_STATUSES),
        STANDOUTS.index(standout) if standout in STANDOUTS else len(STANDOUTS),
    )


def _coerce_reach(reach: Any, index: int, notes: list[str]) -> dict[str, Any]:
    """Normalise one reach block, downgrading any claim it failed to source.

    The repair that matters is the last one: a `direct` or `bridged` status is an
    assertion that two specific people are tied, and it is the assertion the
    operator will act on. Unsourced, it is exactly the fabrication this whole
    prompt is built to prevent — so it is demoted to `surface_only`, which says
    the true thing (here is someone near them, nothing joins them to you yet)
    rather than deleted, which would throw away a real lead.
    """
    if not isinstance(reach, dict):
        notes.append(f"candidates[{index}].reach was not an object; set to cold")
        reach = {}

    for field in ("via", "via_locator", "basis", "source_url", "source_type",
                  "evidence_date"):
        value = reach.get(field)
        reach[field] = value.strip() if isinstance(value, str) else ""

    status = reach.get("status")
    if status not in REACH_STATUSES:
        notes.append(f"candidates[{index}].reach.status was {status!r}; set to cold")
        status = "cold"

    if reach.get("strength") not in STRENGTHS:
        # "" is the contract for the two statuses that assert no tie, so only a
        # sourced reach with a junk strength is worth a note.
        if status in SOURCED_REACH:
            notes.append(
                f"candidates[{index}].reach.strength was "
                f"{reach.get('strength')!r}; set to weak"
            )
            reach["strength"] = "weak"
        else:
            reach["strength"] = ""

    # A hop out of the operator's own export has no public page to cite — the
    # prompt says to leave it empty — so that is the contract being followed.
    # Any other non-http value is a citation that was not really made.
    url = reach.get("source_url")
    if url and not url.lower().startswith(("http://", "https://")):
        notes.append(
            f"candidates[{index}].reach.source_url was not an http(s) URL "
            f"({url!r}); cleared"
        )
        reach["source_url"] = url = ""

    attested = reach.get("source_type") == OPERATOR_SOURCE
    if status in SOURCED_REACH and not reach.get("basis"):
        notes.append(
            f"candidates[{index}].reach claimed {status!r} with no basis; "
            f"downgraded to surface_only"
        )
        status = "surface_only"
        reach["strength"] = ""
    elif status == "bridged" and not reach.get("via"):
        notes.append(
            f"candidates[{index}].reach claimed 'bridged' but named nobody to "
            f"contact; downgraded to surface_only"
        )
        status = "surface_only"
        reach["strength"] = ""
    elif status in SOURCED_REACH and not url and not attested:
        notes.append(
            f"candidates[{index}].reach claimed {status!r} from a public record "
            f"but cited no source; downgraded to surface_only"
        )
        status = "surface_only"
        reach["strength"] = ""

    reach["status"] = status

    hops = reach.get("hops")
    if not isinstance(hops, int) or isinstance(hops, bool):
        hops = 0
    if status == "direct":
        hops = 1
    elif status == "bridged":
        hops = min(max(hops, 2), MAX_REACH_HOPS)
    else:
        hops = 0
    reach["hops"] = hops
    return reach


def _coerce(data: dict[str, Any]) -> list[str]:
    """Normalise shape and enforce what is mechanically checkable.

    Structural junk is repaired in place and recorded; nothing raises. A
    discovery run costs minutes of live search, and losing all of it because one
    candidate came back malformed would be the wrong trade — the repairs are
    reported in `notes` so a prompt regression is visible to whoever is on call
    rather than silently absorbed.
    """
    notes: list[str] = []

    if not isinstance(data.get("interpretation"), dict):
        notes.append("interpretation was not an object; coerced to empty fields")
        data["interpretation"] = {}
    interp = data["interpretation"]
    for field in ("population", "scope", "scope_source"):
        if not isinstance(interp.get(field), str):
            interp[field] = ""
    if interp["scope_source"] not in ("prompt", "origin", "unbounded"):
        interp["scope_source"] = "unbounded" if not interp["scope"] else "prompt"
    if not isinstance(interp.get("excellence_signals"), list):
        interp["excellence_signals"] = []
    interp["excellence_signals"] = [
        s.strip() for s in interp["excellence_signals"] if isinstance(s, str) and s.strip()
    ]

    for field, default in (("brief", ""), ):
        if not isinstance(data.get(field), str):
            data[field] = default
    for field in ("origin_surface", "notes"):
        if not isinstance(data.get(field), list):
            data[field] = []
        data[field] = [s.strip() for s in data[field] if isinstance(s, str) and s.strip()]

    if not isinstance(data.get("candidates"), list):
        notes.append("candidates was not a list; coerced to []")
        data["candidates"] = []

    clean: list[dict[str, Any]] = []
    for i, cand in enumerate(data["candidates"]):
        if not isinstance(cand, dict) or not str(cand.get("name", "")).strip():
            notes.append(f"candidates[{i}] dropped: not an object with a name")
            continue
        for field in ("name", "role", "org", "locator", "why_cracked", "opening"):
            value = cand.get(field)
            cand[field] = value.strip() if isinstance(value, str) else ""

        if cand.get("standout") not in STANDOUTS:
            notes.append(
                f"candidates[{i}].standout was {cand.get('standout')!r}; set to notable"
            )
            cand["standout"] = "notable"

        evidence: list[dict[str, str]] = []
        for item in (cand.get("evidence") if isinstance(cand.get("evidence"), list) else []):
            if not isinstance(item, dict):
                continue
            row = {f: (item.get(f).strip() if isinstance(item.get(f), str) else "")
                   for f in ("object", "source_url", "source_type", "evidence_date")}
            if not row["object"]:
                continue
            if row["source_url"] and not row["source_url"].lower().startswith(
                    ("http://", "https://")):
                notes.append(
                    f"candidates[{i}].evidence source_url was not an http(s) URL "
                    f"({row['source_url']!r}); cleared"
                )
                row["source_url"] = ""
            evidence.append(row)
        cand["evidence"] = evidence

        # An artifact nobody named is the one thing this feature must not ship:
        # without it, "cracked" is just the model's impression of how important
        # someone sounds, which is the ranking it was told not to produce.
        if not evidence and not cand["why_cracked"]:
            notes.append(
                f"candidates[{i}] ({cand['name']}) dropped: no artifact and no "
                f"evidence object — nothing distinguishes them from their peers"
            )
            continue

        cand["reach"] = _coerce_reach(cand.get("reach"), i, notes)
        clean.append(cand)

    clean.sort(key=_rank_key)
    data["candidates"] = clean

    if not isinstance(data.get("searches_used"), int):
        try:
            data["searches_used"] = int(data.get("searches_used", 0))
        except (TypeError, ValueError):
            data["searches_used"] = 0

    return notes


def find_people(
    prompt: str,
    *,
    origin: str = "",
    ask: str = "N/A",
    limit: int = 10,
    max_searches: int = 20,
    origin_connections: list[dict[str, Any]] | None = None,
    origin_co_officers: list[dict[str, Any]] | None = None,
    config: PathfinderConfig | None = None,
    client: Anthropic | None = None,
) -> DiscoveryResult:
    """Find the strongest members of `prompt`'s population reachable from `origin`.

    `origin_connections` is the origin's own imported network, already ranked and
    truncated by the caller — a full export is thousands of rows, which would
    cost more than the search and bury the question. It is only ever passed when
    the origin IS the operator who uploaded it.
    """
    cfg = config or PathfinderConfig()
    # Same long-request timeout as find_path: a search turn is one call that runs
    # for minutes, and the SDK default kills it mid-flight with nothing to show.
    client = client or Anthropic(timeout=cfg.request_timeout_s)

    if max_searches > cfg.search_ceiling and not cfg.pin_max_uses_to_request:
        raise ValueError(
            f"max_searches={max_searches} exceeds search_ceiling={cfg.search_ceiling}"
        )

    payload: dict[str, Any] = {
        "prompt": prompt,
        "origin": origin,
        "ask": (ask or "").strip() or "N/A",
        "limit": limit,
        "max_searches": max_searches,
    }
    if origin_connections:
        payload["origin_connections"] = origin_connections
    if origin_co_officers:
        payload["origin_co_officers"] = origin_co_officers

    system = build_system(cfg)
    tools = build_tools(cfg, max_searches)
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": json.dumps(payload, sort_keys=True, ensure_ascii=False)}
    ]

    usage = UsageRecord()
    started = time.monotonic()
    _progress(
        cfg,
        f"discovering {prompt!r}"
        + (f" reachable from {origin}" if origin else "")
        + f" — up to {max_searches} searches",
    )

    data: dict[str, Any] | None = None
    last_error: Exception | None = None
    for attempt in range(cfg.max_parse_retries + 1):
        message = _run_turn(client, cfg, messages, system, tools, usage)
        _progress(cfg, f"research complete — {usage.web_searches} searches performed")
        try:
            data = extract_json_object(collect_text(message.content))
            break
        except JSONExtractionError as exc:
            last_error = exc
            if attempt == cfg.max_parse_retries:
                break
            usage.parse_retries += 1
            _progress(cfg, "output was not a single JSON object — asking again")
            messages.append({"role": "assistant", "content": message.content})
            messages.append({"role": "user", "content": REFORMAT_INSTRUCTION})

    usage.latency_s = time.monotonic() - started
    if data is None:
        raise UnparseableError(f"no JSON object after retries: {last_error}")

    repairs = _coerce(data)
    # The connections export is private data the operator entrusted to this run.
    # The prompt forbids restating it; this checks rather than trusts, because a
    # leak here is somebody else's email address in a shared board.
    warnings = scan_for_pii(data)

    # searches_used is model-reported; replace it with what actually happened.
    data["searches_used"] = usage.web_searches

    reachable = sum(
        1 for c in data["candidates"]
        if (c.get("reach") or {}).get("status") in SOURCED_REACH
    )
    _progress(
        cfg,
        f"result: {len(data['candidates'])} candidate(s), {reachable} with a "
        f"named route in",
    )

    if cfg.usage_sink:
        record = usage.as_dict(cfg.pricing)
        record.update(ok=True, kind="discovery", prompt=prompt, origin=origin)
        cfg.usage_sink(record)

    return DiscoveryResult(
        data=data,
        usage=usage.as_dict(cfg.pricing),
        validation={"errors": [], "repairs": repairs, "warnings": warnings},
    )


__all__ = [
    "find_people",
    "DiscoveryResult",
    "DISCOVERY_SYSTEM_PROMPT",
    "REACH_STATUSES",
    "STANDOUTS",
    "PathfinderError",
    "RefusalError",
    "FALLBACK_BETA",
]
