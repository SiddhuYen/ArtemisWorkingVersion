# Artemis V6

Finds a **warm-introduction route** between two people, sources every hop, and
displays it on a board.

It is a pure wrapper around Claude with web search. There is one piece of
intelligence in the whole application — `route_engine` → `warm_intro/` — which
asks Claude (using its own `web_search` tool) to research a route and return it
against a fixed schema. No search-API providers, no spaCy, no local relationship
graph, no extraction pipeline.

```
app/          22k lines -> 1.8k     the old engine was deleted, not disabled
dependencies  fastapi, sqlalchemy, anthropic, pydantic
```

## Run it

```bash
python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # set ANTHROPIC_API_KEY
set -a; . ./.env; set +a
.venv/bin/uvicorn app.main:app --port 8079     # http://127.0.0.1:8079/ui/
```

An Anthropic credential is the only thing needed to run a route. A bare
`Anthropic()` also picks up `ANTHROPIC_AUTH_TOKEN` or an `ant auth login`
profile, so `ANTHROPIC_API_KEY` being unset does not mean "no credentials".

## How a route is found

1. **Research.** One Claude call with web search, capped at
   `route_engine.MAX_SEARCHES` (20). Most routes finish in 2–5 searches — the
   model stops as soon as a route passes its hop tests.
2. **Check the shape.** `schema.py` enforces the contract the caller depends on
   — required keys, hop/path reconciliation, the two-intermediary ceiling — and
   repairs what is mechanically derivable (the rating is the weakest hop; a null
   path is `dropped`; the verdict follows the path, since the path is the
   evidence and the verdict is only its label). This is a shape check, not a
   truth check.
3. **Display.** The result is mapped to the board's existing wire contract, so
   the route renders and merges onto the canvas with no UI changes.

**Nothing re-fetches the pages Claude cites.** Each hop carries a `source_url`
that the model says supports it, and reading it is the operator's job. The
citation is a lead, not a verdict.

Every hop must be sourced to a named, specific, public professional connection.
Institutional proximity is not a relationship, and returning *no route* is a
correct result — the caller is about to spend real social capital with a real
person.

There are five outcomes, carried in `verdict`. `PATH_FOUND` is the only one with
a path; the other four are distinct kinds of nothing, kept apart because they are
differently actionable: `NEED_DISAMBIGUATION` (add a role to the context box),
`TARGET_MAP_ONLY` (the target is mapped, but nothing on your side reaches them —
`approach_surface` names who is close), `NO_PATH`, and `INVALID_REQUEST`.

The prompt also enforces a **durability rule**: state the permanent fact, not the
current one. Co-founded is forever; "sits on the board" is a claim about today.
Dates and present-tense titles are allowed only where a search in that run
confirmed them, which is why `evidence_date` is often empty.

## Your own network

Import a LinkedIn `Connections.csv` (IMPORT in the UI, or `POST /network/upload`
with `owner_name`). When a route **starts from you**, the ten connections most
relevant to the target are ranked locally by `contacts.top_candidates()` and
travel in the payload as `starting_person_connections`. Claude may use one as the
first hop without a public source — being in your export *is* the evidence.

Three deliberate limits:

- The shortlist is only sent when the origin matches the uploader. Route from
  anyone else and the key is omitted entirely — not sent empty — so there is
  nothing to accidentally route through. Those contacts are not their
  connections.
- The exemption covers **only** the hop out of you. Every hop onward still needs
  a public, nameable source. Your list says who *you* know; it says nothing about
  who they know.
- A supplied shortlist also makes you a valid starting point. The prompt gates
  each endpoint on whether it can name three facts about them, which an ordinary
  person fails — so `starting_person_connections` overrides that gate for the
  starting person. Without it, a route from someone who isn't a public figure
  comes back `TARGET_MAP_ONLY`.

Hops from your network carry `source_type: "operator_network"` and no
`source_url` — there is nothing public to cite.

The import is optional. It is not a gate.

## Layout

| Path | Role |
|---|---|
| `warm_intro/` | The wrapper: prompt, call, JSON recovery, schema enforcement |
| `app/route_engine.py` | Wrapper → board wire contract; the `search_my_network` tool |
| `app/main.py` | FastAPI surface: routes, jobs, boards, contacts, auth |
| `app/contacts.py` | LinkedIn CSV parsing, owner-scoped storage and search |
| `app/static/` | The board UI |

## Using the wrapper on its own

```python
from warm_intro import find_path

result = find_path({
    "target": "Dana White, CEO of TKO Group Holdings",
    "starting_person": "Charlie Warren, Visiting Partner at Y Combinator",
    "ask": "partnership conversation about a portfolio company",
    "max_searches": 12,
})
result.data        # the schema-conformant route
result.usage       # tokens, cache hits, real search count, cost
result.validation  # errors, repairs and warnings from the shape check
```

There is also a CLI (`warm-intro`).

## Notes for whoever works on this next

- **A route is unverified by construction.** The source verifier was removed —
  nothing in this repo reads the pages Claude cites. `rating` and `strength`
  describe the model's own confidence, not a checked fact, and `source_url` has
  only been shape-checked as an http(s) string. Whoever acts on a route reads
  the sources.
- **The system prompt is the cache prefix.** Treat it as frozen — any byte change
  invalidates the cache and costs a fresh write on the next call.
- **No test suite is committed.** The wrapper was developed against ~80 checks
  (JSON recovery, schema invariants, and a full pipeline against a stubbed
  client). Porting them into `tests/` is the first thing worth doing here.
