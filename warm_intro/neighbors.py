"""Map one person's closest public professional connections.

The pathfinder answers "is there a route from A to B". This answers "who is
around B at all" — the layer-1 expansion its METHOD section performs internally
but never exposes, because its output schema is a single path.

Same discipline, same machinery: one cached system prompt, Claude's own web
search under a hard `max_uses` budget, then every cited URL re-fetched and
checked for both names before the connection is allowed to stand. A connection
whose citation does not name both people is dropped, not downgraded — exactly as
in the pathfinder, and for the same reason: the caller is about to treat this as
fact about a real person.
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
    _accumulate,
    _progress,
    _run_turn,
    build_tools,
)
from .config import PathfinderConfig
from .parsing import JSONExtractionError, collect_text, extract_json_object
from .schema import STRENGTHS, downgrade
from .verify import UrlCheck, verify_hops

NEIGHBORS_SYSTEM_PROMPT = """\
You map one person's closest public professional connections, and you return
structured JSON. You are called by a program, not a person. Never ask
clarifying questions and never write prose outside the JSON object.

# INPUTS

The user message is a JSON object:

{
  "person": string,         // the person to map, usually with a role/org qualifier
  "max_searches": integer   // hard cap
}

# CORE RULE

If a connection cannot be sourced to a named, specific, public professional
relationship, it does not exist. Institutional proximity is not a relationship.

Returning an empty list with a clear reason is a correct and valuable result. A
fabricated connection is a failure — the caller will act on this.

# METHOD

Identify the 5-10 closest professional connections: board colleagues,
co-founders, named co-leads, direct collaborators, funders, appointers,
co-authors, and senior executives who report to or with them. Prefer people whose
relationship to the person is documented in a specific, citable context.

Stop as soon as you have a well-sourced set. Never exceed max_searches.

# CONNECTION TESTS

Every connection must pass all three:

1. A named, citable source places both individuals in the same specific
   professional context.
2. The relationship is current, or there is documented reason it is still live.
3. The source names BOTH people. A page about only one of them is not evidence
   of a relationship.

# INVALID CONNECTIONS — never count these

Same industry. Same city. Same university without confirmed overlap. Same large
conference. Same large employer without both overlapping team and overlapping
dates. Mutual social-platform connection. Shared follower or follow.
Co-appearance on a list or ranking. Being quoted in the same article. "They
probably know each other."

# SOURCING

Prefer primary sources: SEC and regulatory filings, proxy statements, company
leadership and board pages, grant and award records, co-authored publications,
official announcements, first-person accounts. Secondary reporting is acceptable
when it names the specific relationship. Aggregator profiles and
contact-database scrapes are not sufficient on their own.

Record the date of the evidence. A relationship documented only by a role that
ended years ago is weak at best.

# SCOPE LIMIT

Public professional information only. Do not compile or return home addresses,
personal contact details, family relationships, health information, financial
details, or non-professional associations. If the person has no meaningful
public professional footprint, return an empty connections list with the note
"insufficient_public_record" rather than assembling inferences about a private
individual.

# OUTPUT

Return exactly one JSON object. No markdown fences, no preamble, no commentary.

{
  "person": string,
  "person_context": string,        // one line on who they are
  "connections": [
    {
      "name": string,
      "role": string,
      "org": string,
      "relationship_type": string, // e.g. "board colleague", "co-founder", "co-author"
      "basis": string,             // one sentence, specific
      "source_url": string,
      "source_type": string,       // e.g. "sec_filing", "company_page", "reporting"
      "evidence_date": string,     // ISO date or year
      "strength": "strong" | "moderate" | "weak"
    }
  ],
  "notes": [string],
  "searches_used": integer
}
"""

REFORMAT_INSTRUCTION = (
    "Your previous response was not a single parseable JSON object. Do not search "
    "again. Re-emit your findings as exactly one JSON object matching the OUTPUT "
    "schema. No markdown fences, no preamble, no commentary."
)


@dataclass
class NeighborResult:
    data: dict[str, Any]
    usage: dict[str, Any]
    url_checks: list[dict[str, Any]]
    dropped: list[str]


def build_system(cfg: PathfinderConfig) -> list[dict[str, Any]]:
    cache_control: dict[str, Any] = {"type": "ephemeral"}
    if cfg.cache_ttl:
        cache_control["ttl"] = cfg.cache_ttl
    return [
        {"type": "text", "text": NEIGHBORS_SYSTEM_PROMPT, "cache_control": cache_control}
    ]


def _coerce(data: dict[str, Any]) -> list[str]:
    """Normalise shape. Returns repair notes."""
    notes: list[str] = []
    if not isinstance(data.get("connections"), list):
        notes.append("connections was not a list; coerced to []")
        data["connections"] = []
    clean: list[dict[str, Any]] = []
    for i, c in enumerate(data["connections"]):
        if not isinstance(c, dict) or not str(c.get("name", "")).strip():
            notes.append(f"connections[{i}] dropped: not an object with a name")
            continue
        for f in ("role", "org", "relationship_type", "basis", "source_url",
                  "source_type", "evidence_date"):
            c.setdefault(f, "")
        if c.get("strength") not in STRENGTHS:
            notes.append(f"connections[{i}].strength was {c.get('strength')!r}; set to weak")
            c["strength"] = "weak"
        clean.append(c)
    data["connections"] = clean
    if not isinstance(data.get("notes"), list):
        data["notes"] = []
    return notes


def find_neighbors(
    person: str,
    *,
    max_searches: int = 12,
    config: PathfinderConfig | None = None,
    client: Anthropic | None = None,
) -> NeighborResult:
    cfg = config or PathfinderConfig()
    client = client or Anthropic()

    if max_searches > cfg.search_ceiling and not cfg.pin_max_uses_to_request:
        raise ValueError(
            f"max_searches={max_searches} exceeds search_ceiling={cfg.search_ceiling}"
        )

    system = build_system(cfg)
    tools = build_tools(cfg, max_searches)
    payload = {"person": person, "max_searches": max_searches}
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": json.dumps(payload, sort_keys=True, ensure_ascii=False)}
    ]

    usage = UsageRecord()
    started = time.monotonic()
    _progress(cfg, f"mapping {person}'s professional circle — up to {max_searches} searches")

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

    notes = _coerce(data)
    data.setdefault("person", person)
    data.setdefault("person_context", "")

    # Verification. Each connection is a one-hop claim, so it verifies exactly
    # like a pathfinder hop: the cited page must name both people.
    dropped: list[str] = []
    checks: list[UrlCheck] = []
    conns = data["connections"]
    if cfg.verify.enabled and conns:
        _progress(cfg, f"verifying {len(conns)} cited source(s)")
        pseudo_hops = [
            {"from": data["person"], "to": c["name"], "source_url": c.get("source_url", "")}
            for c in conns
        ]
        checks = verify_hops(
            pseudo_hops,
            cfg.verify,
            on_progress=(lambda line: _progress(cfg, line)) if cfg.progress_sink else None,
        )
        kept: list[dict[str, Any]] = []
        for c, check in zip(conns, checks):
            if check.action == "drop":
                dropped.append(
                    f"{c['name']}: {check.note or check.error or check.verdict}"
                )
                continue
            if check.action == "downgrade":
                c["strength"] = downgrade(c.get("strength", "weak"), "weak")
                c["verification"] = check.verdict
            else:
                c["verification"] = "verified"
            kept.append(c)
        data["connections"] = kept

    data["searches_used"] = usage.web_searches
    if notes:
        data["notes"] = list(data.get("notes") or []) + notes
    if dropped:
        data["notes"] = list(data.get("notes") or []) + [
            f"dropped {len(dropped)} unverifiable connection(s)"
        ]

    _progress(cfg, f"result: {len(data['connections'])} verified connection(s)")
    if cfg.usage_sink:
        rec = usage.as_dict(cfg.pricing)
        rec.update(ok=True, kind="neighbors", person=person)
        cfg.usage_sink(rec)

    return NeighborResult(
        data=data,
        usage=usage.as_dict(cfg.pricing),
        url_checks=[c.as_dict() for c in checks],
        dropped=dropped,
    )


__all__ = [
    "find_neighbors",
    "NeighborResult",
    "NEIGHBORS_SYSTEM_PROMPT",
    "PathfinderError",
    "RefusalError",
    "FALLBACK_BETA",
]
