# Artemis V6

Finds a **warm-introduction route** between two people, sources every hop, and
displays it on a board. **Discover** answers the question before it — who that
second person should be.

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

## Discover

Route needs you to already know who you want. Discover doesn't: you name a
population in your own words and it returns the members of it who have actually
built something, ranked by whether your network reaches them.

```bash
curl -X POST localhost:8079/discover -H 'Content-Type: application/json' -d '{
  "prompt": "superintendents",
  "origin_context": "Superintendent of Cabell County Schools, WV",
  "ask": "advice on a district-wide literacy rollout"
}'     # -> {"job_id"}, then poll GET /jobs/{id}
```

Two questions, and a candidate has to answer both. **Is it real?** — the prompt
defines "cracked" operationally: name the artifact they built, shipped, turned
around or moved, and where it is recorded. Seniority, employer prestige,
coverage volume, paid "Top 40" honours, conference talks and follower counts are
called out as the traps they are, because they are exactly what a search returns
first. The model has to write down what excellence looks like for *this*
population — in `interpretation.excellence_signals` — before it names anyone.

**Can you get there?** — every candidate carries a `reach.status`:

| status | means | what you do |
|---|---|---|
| `direct` | in your imported connections, or publicly tied to you | contact them |
| `bridged` | a named person connects you, with a sourced basis | trace the route |
| `surface_only` | we can name who is near them, nothing joins them to you | you may know more than the record does |
| `cold` | no reach found; here on merit | worth recognising, not routable |

Ranking is reach first, then how exceptional they are — a `direct` "strong"
outranks a `cold` "exceptional", because one of them can be acted on today.
Unreachable candidates are still returned, marked, rather than filtered out: you
are frequently the only person who knows about a tie that exists nowhere public.

**Discover proposes, route verifies.** One run sizes reach for a whole
population on roughly the budget a single route spends on one pair, so a
`bridged` candidate carries one sourced sentence, not a checked chain. TRACE
ROUTE on a card places both people on the board, tags them as the page's
endpoints, and opens the route finder — carrying the candidate's role and org
across, without which /connect correctly refuses to run on a bare name.

Two things the shape check enforces, because they are where a costly answer goes
wrong. A `direct` or `bridged` status with no named person or no cited evidence
is **downgraded to `surface_only`**, which says the true thing rather than
deleting a real lead. And a candidate with no artifact and no evidence object is
dropped — without one, "cracked" is only the model's impression of how important
someone sounds, which is the ranking it was told not to produce.

Scope: if your prompt names one ("superintendents *in West Virginia*") it always
wins. If it doesn't, the population is bounded by **your** surface and the UI
says so — an unbounded population resolves to the most famous people in it, who
are precisely the ones nobody can reach. The interpretation ships next to the
answer, because a list of superintendents is a plausible answer to the wrong
question and nothing in the results themselves would show it.

`POST /neighbors` is the old person-expansion — "who is around this named
person" — which held the `/discover` name until this feature took it.

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

Discover reads the same rows under the same rule, and asks a different question
of them: it takes 40, ranked against the population by `contacts.top_by_query()`
rather than against one target, because it needs both the connections who ARE in
that population (they become candidates you already know) and the ones who
merely bridge to it. That ranking singularises the query — prompts are plural
and job titles are not, so `superintendents` has to match a `Superintendent` —
and fills any remaining slots by seniority, since an empty shortlist would report
"you know nobody relevant" when the truth is "nobody's title contains your word".

The import is optional. It is not a gate.

## Layout

| Path | Role |
|---|---|
| `warm_intro/` | The wrapper: prompt, call, JSON recovery, schema enforcement |
| `warm_intro/discovery.py` | The Discover prompt and call: population in, ranked candidates out |
| `app/route_engine.py` | Wrapper → board wire contract; the `search_my_network` tool |
| `app/discover_engine.py` | Discover → board wire contract; roster and registry enrichment |
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

```python
from warm_intro import find_people

result = find_people(
    "software engineers who work on compilers",
    origin="Charlie Warren, Visiting Partner at Y Combinator",
    ask="advice on a build-system rewrite",
    limit=10,
    max_searches=30,
)
result.data["candidates"]      # ranked: reach first, then how exceptional
result.data["interpretation"]  # how the prompt was read, and what bounded it
```

Its system prompt is a separate cache prefix from the pathfinder's, so the two
features keep independent cache lines and neither invalidates the other.

There is also a CLI (`warm-intro`).

## Notes for whoever works on this next

- **A route is unverified by construction.** The source verifier was removed —
  nothing in this repo reads the pages Claude cites. `rating` and `strength`
  describe the model's own confidence, not a checked fact, and `source_url` has
  only been shape-checked as an http(s) string. Whoever acts on a route reads
  the sources.
- **A Discover reach claim is weaker than a route, on purpose.** It is one
  sourced sentence produced while mapping a whole population, not a checked
  chain. `bridged` means "there is a named person and a named object here" — it
  does not mean the route has been traced. Trace it before you act on it.
- **The system prompts are the cache prefixes.** Treat both as frozen — any byte
  change invalidates that feature's cache and costs a fresh write on the next
  call. `prompt.py` and `discovery.py` are separate prefixes and do not affect
  each other.
- **`tests/` covers the boundaries, not the model.** Registry parsing, org-name
  extraction, scraped-name validation, owner scoping, and Discover's shape check
  and roster ranking — including a full `find_people` pass against a stubbed
  client. Nothing here evaluates answer quality; that still needs a live run
  against known people. `.venv/bin/python -m pytest tests/ -q`
