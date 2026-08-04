# Artemis V6

Finds a **warm-introduction route** between two people, sources every hop, and
displays it on a board.

It is a pure wrapper around Claude with web search. There is one piece of
intelligence in the whole application — `route_engine` → `warm_intro/` — which
asks Claude (using its own `web_search` tool) to research a route, then
independently re-fetches every page it cites to check the claim. No search-API
providers, no spaCy, no local relationship graph, no extraction pipeline.

```
app/          22k lines -> 1.8k     the old engine was deleted, not disabled
dependencies  fastapi, sqlalchemy, anthropic, httpx, pydantic
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
2. **Verify.** Every cited URL is re-fetched and checked for both names in the
   hop. Verification *annotates*, it never vetoes: a failed check weakens the
   hop and records why, and the route still comes back with its evidence
   attached. Flip `VerifyConfig.actions["names_missing"]` to `"drop"` if you'd
   rather have silence than a route you must check yourself.
3. **Display.** The result is mapped to the board's existing wire contract, so
   the route renders and merges onto the canvas with no UI changes.

Every hop must be sourced to a named, specific, public professional connection.
Institutional proximity is not a relationship, and returning *no route* is a
correct result — the caller is about to spend real social capital with a real
person.

## Your own network

Import a LinkedIn `Connections.csv` (IMPORT in the UI, or `POST /network/upload`
with `owner_name`). When a route **starts from you**, Claude gets a
`search_my_network` tool and can use a first-degree connection as the first hop
without a public source — being in your export *is* the evidence.

Two deliberate limits:

- The tool is only offered when the origin matches the uploader. Route from
  anyone else and it isn't passed at all, so there is nothing to accidentally
  route through — those contacts are not their connections.
- The exemption covers **only** the hop out of you. Every hop onward still needs
  a public, citable source. Your list says who *you* know; it says nothing about
  who they know.

Hops from your network carry `source_type: "operator_network"` and no
`source_url`, and skip URL verification — there is nothing public to check.

The import is optional. It is not a gate.

## Layout

| Path | Role |
|---|---|
| `warm_intro/` | The wrapper: prompt, call, JSON recovery, schema enforcement, source verification |
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
result.url_checks  # per-hop fetch verdicts
```

There is also a CLI (`warm-intro`), including `--verify-only`, which re-runs the
schema and source checks over an existing result **without an API call** —
useful for re-checking a stored route before acting on it, since links rot and
roles end.

## Notes for whoever works on this next

- **Set a real `VerifyConfig.user_agent`.** `sec.gov` and `wikipedia.org` serve
  **403** to a spoofed browser agent and 200 to a descriptive one — and those are
  exactly the primary sources the prompt prefers. A fake Chrome UA silently turns
  verifiable filings into `unreachable` downgrades.
- **Given names are matched loosely, surnames exactly.** A TKO proxy says
  "Ariel Emanuel" 28 times and "Ari Emanuel" never; primary sources use legal
  names. Matching is done on the token *adjacent* to the surname, so a page
  containing "Rahm Emanuel" and "Ari Gold" separately does not count as a match.
- **The system prompt is the cache prefix.** Treat it as frozen — any byte change
  invalidates the cache and costs a fresh write on the next call.
- **No test suite is committed.** The wrapper was developed against ~80 checks
  (JSON recovery, schema invariants, name matching, live fetch verdicts, and a
  full pipeline against a stubbed client). Porting them into `tests/` is the
  first thing worth doing here.
