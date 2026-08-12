#!/usr/bin/env python
"""Measure what the state-registry lookup actually returns for real people.

Registry only. No Anthropic call, no web search, no model, no cost.

    .venv/bin/python scripts/registry_hitrate.py subjects.csv [--json out.json]

Input CSV columns: name, context, state, source
  name    the person to look up
  context the free-text string an operator would type into Artemis's context
          box — this is the ONLY thing the org guesser gets, deliberately, so
          that context->org extraction is measured and not bypassed
  state   expected state code, used only to check state_from_text() agreed
  source  where the subject came from; printed so the sample can be audited

For each subject it reports the four pipeline stages separately, because "no
co-officers" has four very different causes and they need different fixes:

  1. state detected from context?
  2. orgs guessed from context?          <- org_candidates()
  3. entities found for those orgs?      <- the state's search form
  4. was the subject NAMED on a filing?  <- the invariant; no name, no hop

A co-officer is also classified human vs non-human. A registered agent, a law
firm, or a corporate-services company is not a warm introduction path, and
counting one as a hit would overstate the result.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import registry  # noqa: E402

# --- classifying what came back ----------------------------------------------
# A registered agent is a role the state assigns to whoever accepts service of
# process. It is frequently a law firm or a commercial agent service, and even
# when it is a natural person it is a paid service relationship, not a
# colleague. Counted separately, never as a warm path.
_AGENT_ROLES = re.compile(r"agent", re.I)

# Entity-shaped names: these are companies, not people.
_ENTITY_MARKERS = re.compile(
    r"\b(inc|incorporated|llc|l\.l\.c|ltd|corp|corporation|company|co|pllc|pc|p\.c|"
    r"llp|lp|associates|group|services|service|systems|solutions|holdings|trust|"
    r"bank|agency|registered agent|agents?|law|legal|attorney|attorneys|firm|"
    r"csc|ct corporation|corporation service|national registered|incorp|"
    r"cogency|vcorp|harvard business|northwest registered)\b", re.I)

_COMMERCIAL_AGENTS = re.compile(
    r"(ct corporation|corporation service|csc|national registered agents|"
    r"cogency global|vcorp|registered agent|incorp services|harvard business|"
    r"northwest registered|united states corporation|paracorp|capitol services)",
    re.I)


def classify(role: str, name: str) -> str:
    """-> 'human' | 'agent' | 'entity'"""
    if _COMMERCIAL_AGENTS.search(name):
        return "entity"
    if _AGENT_ROLES.search(role or ""):
        # A named human can be a registered agent; still not a warm path.
        return "agent"
    if _ENTITY_MARKERS.search(name or ""):
        return "entity"
    return "human"


def probe(name: str, context: str, expected_state: str) -> dict:
    """Run the production lookup, then gather stage diagnostics separately.

    Two fetchers on purpose. The measured pass calls the real co_officers() with
    its own request budget, so latency and HTTP counts are what a build would
    actually spend. The diagnostic pass re-derives per-org entity detail on a
    SEPARATE budget — sharing one fetcher let the diagnostics eat the per-host
    cap and starve the real lookup, which silently reported 0 co-officers for
    every subject in the more request-hungry state.
    """
    state = registry.state_from_text(context)
    orgs = registry.org_candidates(context)
    implemented = bool(state and state.upper() in registry.REGISTRIES)

    result: list[dict] = []
    error = None

    # --- measured pass: the real thing, on its own budget --------------------
    fm = registry.new_fetcher()
    t0 = time.monotonic()
    try:
        if implemented:
            result = registry.co_officers(name, context, fetcher=fm)
    except Exception as exc:                                  # pragma: no cover
        error = f"{type(exc).__name__}: {exc}"
    secs = time.monotonic() - t0
    http_calls, hosts = len(fm.calls), dict(fm.counts)
    fm.client.close()

    # --- diagnostic pass: which stage blocked, on a separate budget ----------
    entities: dict[str, list[str]] = {}
    named_on: list[str] = []
    officers_seen = 0
    fd = registry.new_fetcher()
    fd.cap = 1000                       # diagnostics are not a build; don't cap
    try:
        if implemented:
            key = registry._norm(name)
            for org in orgs:
                try:
                    offs = registry.officers_for_org(org, state, fetcher=fd)
                except Exception as exc:                      # pragma: no cover
                    entities[org] = [f"ERROR {type(exc).__name__}"]
                    continue
                officers_seen += len(offs)
                by_entity: dict[str, list] = {}
                for o in offs:
                    by_entity.setdefault(o.org_id, []).append(o)
                entities[org] = [v[0].org_name for v in by_entity.values()]
                for _oid, group in by_entity.items():
                    if any(registry._norm(o.name) == key for o in group):
                        named_on.append(group[0].org_name)
    except Exception:                                         # pragma: no cover
        pass
    finally:
        fd.client.close()

    buckets = {"human": [], "agent": [], "entity": []}
    for c in result:
        buckets[classify(c.get("role", ""), c.get("name", ""))].append(c)

    return {
        "name": name, "context": context,
        "state_expected": expected_state, "state_detected": state,
        "state_ok": (state or "").upper() == (expected_state or "").upper(),
        "implemented": bool(state and state.upper() in registry.REGISTRIES),
        "orgs_guessed": orgs, "n_orgs": len(orgs),
        "entities_per_org": entities,
        "n_entities": sum(len(v) for v in entities.values()),
        "officers_seen_before_invariant": officers_seen,
        "named_on_filings": named_on, "named_on_any": bool(named_on),
        "co_officers": result, "n_co_officers": len(result),
        "n_human": len(buckets["human"]), "n_agent": len(buckets["agent"]),
        "n_entity": len(buckets["entity"]),
        "human_names": [c["name"] for c in buckets["human"]],
        "agent_names": [c["name"] for c in buckets["agent"]],
        "entity_names": [c["name"] for c in buckets["entity"]],
        "http_calls": http_calls, "hosts": hosts,
        "secs": round(secs, 2), "error": error,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", type=Path)
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    rows = list(csv.DictReader(args.csv.open(encoding="utf-8")))
    if args.limit:
        rows = rows[: args.limit]

    out = []
    print(f"{'#':<3}{'SUBJECT':<26}{'ST':<4}{'ORGS':<5}{'ENT':<5}{'NAMED':<7}"
          f"{'CO':<4}{'HUM':<5}{'AGT':<5}{'ENT2':<6}{'HTTP':<6}{'SECS':<7}")
    print("-" * 92)
    for i, r in enumerate(rows, 1):
        d = probe(r["name"].strip(), r["context"].strip(), r.get("state", "").strip())
        d["source"] = r.get("source", "")
        out.append(d)
        print(f"{i:<3}{d['name'][:24]:<26}{str(d['state_detected']):<4}"
              f"{d['n_orgs']:<5}{d['n_entities']:<5}{('YES' if d['named_on_any'] else 'no'):<7}"
              f"{d['n_co_officers']:<4}{d['n_human']:<5}{d['n_agent']:<5}{d['n_entity']:<6}"
              f"{d['http_calls']:<6}{d['secs']:<7.1f}")

    n = len(out)
    hits_any = [d for d in out if d["n_co_officers"] >= 1]
    hits_hum = [d for d in out if d["n_human"] >= 1]
    hits3_hum = [d for d in out if d["n_human"] >= 3]
    tot_co = sum(d["n_co_officers"] for d in out)
    tot_h = sum(d["n_human"] for d in out)
    tot_a = sum(d["n_agent"] for d in out)
    tot_e = sum(d["n_entity"] for d in out)

    def pct(x): return f"{x}/{n} ({x/n*100:.0f}%)" if n else "0"

    print("\n" + "=" * 92)
    print("AGGREGATE")
    print(f"  subjects                                  {n}")
    print(f"  state detected & state implemented        {pct(sum(d['implemented'] for d in out))}")
    print(f"  >=1 org guessed from context              {pct(sum(d['n_orgs'] > 0 for d in out))}")
    print(f"  >=1 entity found by search                {pct(sum(d['n_entities'] > 0 for d in out))}")
    print(f"  NAMED on >=1 filing (the invariant)       {pct(sum(d['named_on_any'] for d in out))}")
    print(f"  >=1 co-officer of ANY kind                {pct(len(hits_any))}")
    print(f"  >=1 HUMAN co-officer  <- the real number  {pct(len(hits_hum))}")
    print(f"  >=3 HUMAN co-officers                     {pct(len(hits3_hum))}")
    if hits_hum:
        med = statistics.median([d["n_human"] for d in hits_hum])
        print(f"  median human co-officers per hit          {med:.0f}")
    print(f"  median latency (all subjects)             "
          f"{statistics.median([d['secs'] for d in out]):.1f}s")
    print(f"  median http calls per subject             "
          f"{statistics.median([d['http_calls'] for d in out]):.0f}")
    print(f"\n  co-officers returned, by kind:")
    print(f"     human                                  {tot_h}"
          f"{f'  ({tot_h/tot_co*100:.0f}%)' if tot_co else ''}")
    print(f"     registered agent (NOT a warm path)     {tot_a}"
          f"{f'  ({tot_a/tot_co*100:.0f}%)' if tot_co else ''}")
    print(f"     entity/law firm (NOT a warm path)      {tot_e}"
          f"{f'  ({tot_e/tot_co*100:.0f}%)' if tot_co else ''}")
    print(f"     TOTAL                                  {tot_co}")

    print("\n  per-state:")
    print(f"     {'ST':<4}{'n':<4}{'named':<7}{'hit(human)':<12}{'co-off':<8}"
          f"{'human':<7}{'agent':<7}{'entity':<7}{'med s':<7}")
    states = sorted({(d["state_detected"] or "?") for d in out})
    for st in states:
        g = [d for d in out if (d["state_detected"] or "?") == st]
        print(f"     {st:<4}{len(g):<4}{sum(d['named_on_any'] for d in g):<7}"
              f"{sum(d['n_human'] >= 1 for d in g):<12}"
              f"{sum(d['n_co_officers'] for d in g):<8}"
              f"{sum(d['n_human'] for d in g):<7}{sum(d['n_agent'] for d in g):<7}"
              f"{sum(d['n_entity'] for d in g):<7}"
              f"{statistics.median([d['secs'] for d in g]):<7.1f}")

    # caching question: how many DISTINCT orgs did the whole set hit?
    all_orgs = [o for d in out for o in d["orgs_guessed"]]
    all_ent = [e for d in out for v in d["entities_per_org"].values() for e in v]
    print(f"\n  distinct org strings searched             "
          f"{len(set(x.lower() for x in all_orgs))} (of {len(all_orgs)} lookups)")
    print(f"  distinct entities retrieved               "
          f"{len(set(x.lower() for x in all_ent))} (of {len(all_ent)} retrievals)")

    print("\n  FAILURE ATTRIBUTION (first blocking stage per miss):")
    stages = {"state not implemented": 0, "no org guessed": 0,
              "search found no entity": 0, "subject not named on filing": 0,
              "named but no OTHER officer": 0, "hit": 0}
    for d in out:
        if d["n_human"] >= 1:
            stages["hit"] += 1
        elif not d["implemented"]:
            stages["state not implemented"] += 1
        elif d["n_orgs"] == 0:
            stages["no org guessed"] += 1
        elif d["n_entities"] == 0:
            stages["search found no entity"] += 1
        elif not d["named_on_any"]:
            stages["subject not named on filing"] += 1
        else:
            stages["named but no OTHER officer"] += 1
    for k, v in stages.items():
        print(f"     {k:<32} {v:>3}  {'#'*v}")

    if args.json:
        args.json.write_text(json.dumps(out, indent=2))
        print(f"\n  wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
