"""Tests for Discover: the shape check, the roster ranking, and owner scoping.

The load-bearing half of this file is the reach downgrades. A candidate's
`reach.status` is the field the operator acts on — `bridged` means "email this
named person about them" — so a status the model asserted without naming the
person or the evidence is the one failure here that costs a real relationship.
`_coerce` demotes those to `surface_only`, which says the true thing, and these
tests are what keep that from quietly regressing into a pass-through.

Run: .venv/bin/python -m pytest tests/test_discovery.py -q
"""
from __future__ import annotations

import importlib

import pytest

from warm_intro.discovery import _coerce, _rank_key, find_people
from warm_intro.config import PathfinderConfig


def _candidate(**over):
    """A minimal candidate that passes the shape check untouched."""
    base = {
        "name": "Dana Ruiz",
        "role": "runs a mid-size district",
        "org": "Cabell County Schools",
        "locator": "",
        "why_cracked": "Took the district's graduation rate from 68% to 91%.",
        "evidence": [{"object": "state report card, 2019-2024",
                      "source_url": "https://example.gov/report",
                      "source_type": "state_filing", "evidence_date": "2024"}],
        "standout": "exceptional",
        "reach": {"status": "cold", "via": "", "via_locator": "", "basis": "",
                  "source_url": "", "source_type": "", "evidence_date": "",
                  "hops": 0, "strength": ""},
        "opening": "",
    }
    base.update(over)
    return base


def _doc(*candidates):
    return {
        "brief": "b",
        "interpretation": {"population": "superintendents", "scope": "WV",
                           "scope_source": "prompt", "excellence_signals": ["x"]},
        "origin_surface": ["a"],
        "candidates": list(candidates),
        "notes": [],
        "searches_used": 3,
    }


# ------------------------------------------------------- reach must be sourced
def test_bridged_without_a_basis_is_downgraded():
    """A bridge with nobody's evidence behind it is not a bridge."""
    data = _doc(_candidate(reach={"status": "bridged", "via": "Sam Ali",
                                  "basis": "", "source_url": "https://x.test/a",
                                  "strength": "strong", "hops": 2}))
    notes = _coerce(data)

    assert data["candidates"][0]["reach"]["status"] == "surface_only"
    assert data["candidates"][0]["reach"]["strength"] == ""
    assert any("no basis" in n for n in notes)


def test_bridged_without_a_named_person_is_downgraded():
    """'One intro away' from whom? Without a name there is nothing to act on."""
    data = _doc(_candidate(reach={"status": "bridged", "via": "",
                                  "basis": "They serve on the same consortium.",
                                  "source_url": "https://x.test/a",
                                  "strength": "moderate", "hops": 2}))
    notes = _coerce(data)

    assert data["candidates"][0]["reach"]["status"] == "surface_only"
    assert any("named nobody to contact" in n for n in notes)


def test_public_record_reach_without_a_citation_is_downgraded():
    data = _doc(_candidate(reach={"status": "direct", "via": "",
                                  "basis": "They co-chaired the state task force.",
                                  "source_url": "", "source_type": "reporting",
                                  "strength": "strong", "hops": 1}))
    notes = _coerce(data)

    assert data["candidates"][0]["reach"]["status"] == "surface_only"
    assert any("cited no source" in n for n in notes)


def test_operator_network_reach_keeps_its_empty_url():
    """A hop out of the operator's own export has no public page to cite.

    The empty `source_url` there is the contract being followed, not broken, so
    it must survive the check that demotes every other uncited reach.
    """
    data = _doc(_candidate(reach={
        "status": "direct", "via": "", "basis": "In your imported connections.",
        "source_url": "", "source_type": "operator_network",
        "evidence_date": "2019-04-02", "strength": "strong", "hops": 1}))
    _coerce(data)

    reach = data["candidates"][0]["reach"]
    assert reach["status"] == "direct"
    assert reach["strength"] == "strong"
    assert reach["source_url"] == ""


def test_non_http_reach_url_is_cleared_and_downgraded():
    data = _doc(_candidate(reach={"status": "bridged", "via": "Sam Ali",
                                  "basis": "Co-organized the same nonprofit.",
                                  "source_url": "sources say so",
                                  "source_type": "reporting",
                                  "strength": "strong", "hops": 2}))
    notes = _coerce(data)

    assert data["candidates"][0]["reach"]["source_url"] == ""
    assert data["candidates"][0]["reach"]["status"] == "surface_only"
    assert any("not an http(s) URL" in n for n in notes)


def test_hops_are_derived_not_trusted():
    """`hops` is display arithmetic, so it follows the status rather than the model."""
    data = _doc(
        _candidate(name="A", reach={"status": "direct", "basis": "In your export.",
                                    "source_type": "operator_network",
                                    "source_url": "", "strength": "strong",
                                    "hops": 4}),
        _candidate(name="B", reach={"status": "bridged", "via": "Sam Ali",
                                    "basis": "Co-founded a nonprofit together.",
                                    "source_url": "https://x.test/a",
                                    "strength": "strong", "hops": 9}),
        _candidate(name="C", reach={"status": "cold", "basis": "", "hops": 7}),
    )
    _coerce(data)

    by_name = {c["name"]: c for c in data["candidates"]}
    assert by_name["A"]["reach"]["hops"] == 1
    assert by_name["B"]["reach"]["hops"] == 3   # clamped to the ceiling
    assert by_name["C"]["reach"]["hops"] == 0


# ------------------------------------------------------ excellence must be named
def test_candidate_with_no_artifact_is_dropped():
    """Without an artifact, "cracked" is just how important someone sounds."""
    data = _doc(_candidate(name="Nobody Inparticular", why_cracked="", evidence=[]))
    notes = _coerce(data)

    assert data["candidates"] == []
    assert any("no artifact" in n for n in notes)


def test_candidate_with_a_stated_artifact_but_no_link_survives():
    """A real object the model could not link is still worth reporting."""
    data = _doc(_candidate(evidence=[{"object": "district board minutes",
                                      "source_url": "", "source_type": "minutes",
                                      "evidence_date": ""}]))
    _coerce(data)
    assert len(data["candidates"]) == 1


def test_unnamed_candidates_are_dropped():
    data = _doc(_candidate(name="   "), _candidate(name="Real Person"))
    notes = _coerce(data)

    assert [c["name"] for c in data["candidates"]] == ["Real Person"]
    assert any("not an object with a name" in n for n in notes)


def test_unknown_standout_falls_back_to_notable():
    data = _doc(_candidate(standout="legendary"))
    notes = _coerce(data)

    assert data["candidates"][0]["standout"] == "notable"
    assert any("standout" in n for n in notes)


# ----------------------------------------------------------------- the ranking
def test_reachability_outranks_brilliance():
    """A `direct` "strong" beats a `cold` "exceptional" — one can be acted on today."""
    data = _doc(
        _candidate(name="Famous", standout="exceptional",
                   reach={"status": "cold", "basis": "", "hops": 0}),
        _candidate(name="Reachable", standout="strong",
                   reach={"status": "direct", "basis": "In your export.",
                          "source_type": "operator_network", "source_url": "",
                          "strength": "strong", "hops": 1}),
        _candidate(name="Bridged", standout="notable",
                   reach={"status": "bridged", "via": "Sam Ali",
                          "basis": "Co-founded a nonprofit together.",
                          "source_url": "https://x.test/a",
                          "strength": "moderate", "hops": 2}),
    )
    _coerce(data)

    assert [c["name"] for c in data["candidates"]] == ["Reachable", "Bridged", "Famous"]


def test_ranking_breaks_ties_on_standout():
    data = _doc(
        _candidate(name="Second", standout="notable",
                   reach={"status": "cold", "basis": "", "hops": 0}),
        _candidate(name="First", standout="exceptional",
                   reach={"status": "cold", "basis": "", "hops": 0}),
    )
    _coerce(data)
    assert [c["name"] for c in data["candidates"]] == ["First", "Second"]


def test_rank_key_puts_unknown_values_last():
    assert _rank_key({"reach": {"status": "?"}, "standout": "?"}) > _rank_key(_candidate())


# ------------------------------------------------------------- interpretation
def test_missing_interpretation_is_repaired_not_fatal():
    data = {"candidates": [], "brief": None}
    notes = _coerce(data)

    assert data["interpretation"]["population"] == ""
    assert data["interpretation"]["scope_source"] == "unbounded"
    assert data["brief"] == ""
    assert any("interpretation" in n for n in notes)


def test_scope_source_is_derived_when_junk():
    data = _doc()
    data["interpretation"]["scope_source"] = "vibes"
    _coerce(data)
    # A scope was stated, so the honest fallback is that the prompt carried it.
    assert data["interpretation"]["scope_source"] == "prompt"


# ------------------------------------------------------------ roster ranking
@pytest.fixture()
def contacts_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEMIS_BOARDS_DB_URL", f"sqlite:///{tmp_path}/boards.db")
    import app.config, app.db, app.models, app.contacts
    for mod in (app.config, app.db, app.models, app.contacts):
        importlib.reload(mod)
    app.db.init_boards_db()
    return app.db, app.contacts


def _seed_contacts(db_mod, contacts_mod, rows):
    session = db_mod.BoardsSessionLocal()
    contacts_mod.ingest(session, "owner-1", rows)
    return session, contacts_mod


def test_top_by_query_matches_the_singular_title(contacts_db):
    """The whole reason this ranking exists: prompts are plural, titles are not."""
    db_mod, contacts_mod = contacts_db
    rows = [{"name": f"Filler {i}", "title": "Analyst", "company": "Acme"}
            for i in range(60)]
    rows.append({"name": "Dana Ruiz", "title": "Superintendent",
                 "company": "Cabell County Schools"})
    session, _ = _seed_contacts(db_mod, contacts_mod, rows)

    got = contacts_mod.top_by_query(session, "owner-1", "superintendents", limit=5)
    assert "Dana Ruiz" in [c["name"] for c in got]


def test_top_by_query_fills_with_seniority_when_nothing_matches(contacts_db):
    """An empty shortlist would report "you know nobody relevant" when the truth
    is "nobody's title contains your word" — and the senior ones are the bridges."""
    db_mod, contacts_mod = contacts_db
    rows = [{"name": f"Junior {i}", "title": "Analyst", "company": "Acme"}
            for i in range(60)]
    rows.append({"name": "Chris Board", "title": "President",
                 "company": "State School Boards Association"})
    session, _ = _seed_contacts(db_mod, contacts_mod, rows)

    got = contacts_mod.top_by_query(session, "owner-1", "superintendents", limit=5)
    assert len(got) == 5
    assert got[0]["name"] == "Chris Board"


def test_top_by_query_returns_everyone_below_the_limit(contacts_db):
    db_mod, contacts_mod = contacts_db
    session, _ = _seed_contacts(db_mod, contacts_mod, [
        {"name": "A Person", "title": "Analyst"},
        {"name": "B Person", "title": "Engineer"},
    ])
    got = contacts_mod.top_by_query(session, "owner-1", "superintendents", limit=40)
    assert len(got) == 2


def test_top_by_query_refuses_an_ownerless_read(contacts_db):
    db_mod, contacts_mod = contacts_db
    session = db_mod.BoardsSessionLocal()
    with pytest.raises(ValueError):
        contacts_mod.top_by_query(session, "", "superintendents")


# ---------------------------------------------------------------- owner scoping
def test_roster_travels_only_when_the_origin_is_the_operator(contacts_db, monkeypatch):
    """The connections export is evidence about the uploader and nobody else.

    Discovering from someone else's name must omit the key entirely rather than
    send it empty — there is then nothing to accidentally search through.
    """
    db_mod, contacts_mod = contacts_db
    session, _ = _seed_contacts(db_mod, contacts_mod,
                                [{"name": "Dana Ruiz", "title": "Superintendent"}])

    import app.discover_engine as de
    importlib.reload(de)

    seen: list[dict] = []

    def fake_find_people(prompt, **kwargs):
        seen.append(kwargs)
        class _R:
            data = {"candidates": [], "notes": ["none"], "interpretation": {}}
            usage = {}
            validation = {"errors": [], "repairs": [], "warnings": []}
        return _R()

    monkeypatch.setattr(de, "find_people", fake_find_people)
    monkeypatch.setattr(de.registry, "co_officers", lambda *a, **k: [])

    de.run_discover("superintendents", "Alice Example", db=session,
                    operator_id="owner-1", operator_name="Alice Example")
    assert seen[-1]["origin_connections"], "the operator's own list should travel"

    de.run_discover("superintendents", "Someone Else", db=session,
                    operator_id="owner-1", operator_name="Alice Example")
    assert seen[-1]["origin_connections"] is None, \
        "another person's origin must not borrow the operator's connections"


def test_limit_is_clamped(contacts_db, monkeypatch):
    import app.discover_engine as de
    importlib.reload(de)

    seen: list[dict] = []

    def fake_find_people(prompt, **kwargs):
        seen.append(kwargs)
        class _R:
            data = {"candidates": [], "notes": ["none"], "interpretation": {}}
            usage = {}
            validation = {"errors": [], "repairs": [], "warnings": []}
        return _R()

    monkeypatch.setattr(de, "find_people", fake_find_people)
    monkeypatch.setattr(de.registry, "co_officers", lambda *a, **k: [])

    de.run_discover("superintendents", limit=9999)
    assert seen[-1]["limit"] == de.MAX_LIMIT
    de.run_discover("superintendents", limit=0)
    assert seen[-1]["limit"] == de.DEFAULT_LIMIT


# ------------------------------------------------- the whole call, no network
class _Block:
    def __init__(self, text):
        self.type, self.text = "text", text


class _Usage:
    input_tokens = 1200
    output_tokens = 400
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 900
    server_tool_use = type("S", (), {"web_search_requests": 7})()
    iterations = []


class _Message:
    stop_reason = "end_turn"
    model = "claude-opus-5"
    container = None

    def __init__(self, text):
        self.content = [_Block(text)]
        self.usage = _Usage()


class _StubClient:
    """Stands in for Anthropic, and records what the wrapper actually sent."""

    def __init__(self, text):
        self.sent = {}
        outer = self

        class _Stream:
            def __init__(self, **kwargs):
                outer.sent = kwargs

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get_final_message(self):
                return _Message(text)

        self.beta = type("B", (), {"messages": type("M", (), {"stream": _Stream})()})()


def test_find_people_builds_the_request_and_checks_the_result():
    """One pass over the real call path: payload in, shape-checked object out."""
    payload_back = """{
      "brief": "Six WV superintendents with documented turnarounds.",
      "interpretation": {"population": "public school district superintendents",
                         "scope": "West Virginia", "scope_source": "prompt",
                         "excellence_signals": ["measured outcome against a baseline"]},
      "origin_surface": ["Cabell County education"],
      "candidates": [
        {"name": "Dana Ruiz", "role": "runs a mid-size district", "org": "Cabell County Schools",
         "locator": "", "why_cracked": "Took graduation from 68% to 91%.",
         "evidence": [{"object": "state report card", "source_url": "https://example.gov/r",
                       "source_type": "state_filing", "evidence_date": "2024"}],
         "standout": "exceptional",
         "reach": {"status": "bridged", "via": "Sam Ali", "via_locator": "board president",
                   "basis": "Co-organized the county literacy consortium with her.",
                   "source_url": "https://example.gov/c", "source_type": "reporting",
                   "evidence_date": "2023", "hops": 2, "strength": "moderate"},
         "opening": "Ask Sam to forward the literacy pilot memo."}
      ],
      "notes": ["aimed for 10, 1 passed"],
      "searches_used": 99
    }"""
    client = _StubClient(payload_back)
    cfg = PathfinderConfig(search_ceiling=30, enable_web_fetch=True)

    result = find_people(
        "superintendents", origin="Alice Example, Cabell County", ask="advice",
        limit=10, max_searches=30, config=cfg, client=client,
        origin_connections=[{"name": "Sam Ali", "title": "President",
                             "company": "County Board", "connected_on": "2019"}],
    )

    # what went out
    sent = client.sent
    assert sent["model"] == cfg.model
    assert [t["name"] for t in sent["tools"]] == ["web_search", "web_fetch"]
    assert all(t["max_uses"] == 30 for t in sent["tools"])
    assert sent["system"][0]["cache_control"]["type"] == "ephemeral"
    body = sent["messages"][0]["content"]
    assert '"prompt": "superintendents"' in body
    assert '"origin_connections"' in body

    # what came back
    data = result.data
    assert data["candidates"][0]["reach"]["status"] == "bridged"
    assert data["candidates"][0]["reach"]["hops"] == 2
    # model-reported search counts are replaced with what actually happened
    assert data["searches_used"] == 7
    assert result.usage["cache_hit"] is True


def test_find_people_refuses_a_budget_over_the_ceiling():
    with pytest.raises(ValueError):
        find_people("x", max_searches=99, config=PathfinderConfig(search_ceiling=30),
                    client=_StubClient("{}"))


def test_find_people_survives_prose_around_the_json():
    """The recovery path: models wrap objects in fences more often than they should."""
    client = _StubClient('Here you go:\n```json\n{"candidates": []}\n```')
    result = find_people("x", max_searches=5,
                         config=PathfinderConfig(search_ceiling=30), client=client)
    assert result.data["candidates"] == []


# ------------------------------------------------------------------ the wire
TOKEN = "tok_alice_aaaaaaaaaaaaaaaaaaaaaaaaaaaa"


@pytest.fixture()
def api(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    monkeypatch.setenv("ARTEMIS_OPERATORS", f"alice:Alice Example:{TOKEN}")
    monkeypatch.setenv("ARTEMIS_BOARDS_DB_URL", f"sqlite:///{tmp_path}/boards.db")
    monkeypatch.delenv("ARTEMIS_ACCESS_TOKEN", raising=False)

    import app.config, app.db, app.models, app.auth, app.contacts, app.main
    for mod in (app.config, app.db, app.models, app.auth, app.contacts, app.main):
        importlib.reload(mod)
    app.main.init_boards_db()

    from fastapi.testclient import TestClient
    client = TestClient(app.main.app)
    client.headers.update({"Authorization": f"Bearer {TOKEN}"})
    return app.main, client


def test_discover_requires_a_prompt(api):
    _, client = api
    assert client.post("/discover", json={}).status_code == 400
    assert client.post("/discover", json={"prompt": "   "}).status_code == 400


def test_discover_rejects_a_non_numeric_limit(api):
    _, client = api
    r = client.post("/discover", json={"prompt": "superintendents", "limit": "lots"})
    assert r.status_code == 400


def test_discover_defaults_the_origin_to_the_session_identity(api, monkeypatch):
    """Who is asking comes from the token, not the body.

    The body may still name an origin — searching from someone else is a real
    request — but it decides only whose public surface to search from. Whose
    contacts may be lent out is settled by `op.name`, which the caller cannot set.
    """
    main, client = api
    started: list[tuple] = []
    monkeypatch.setattr(main, "_start_build_job",
                        lambda request, kind, worker, args: started.append(args) or {"job_id": "x"})

    client.post("/discover", json={"prompt": "superintendents"})
    prompt, origin, ask, ctx, limit, op_id, op_name = started[-1]
    assert (origin, op_id, op_name) == ("Alice Example", "alice", "Alice Example")

    client.post("/discover", json={"prompt": "superintendents", "origin": "Mallory"})
    _, origin, _, _, _, op_id, op_name = started[-1]
    assert origin == "Mallory"
    assert (op_id, op_name) == ("alice", "Alice Example"), \
        "identity must not follow the body"


def test_neighbors_still_answers_the_old_person_expansion(api, monkeypatch):
    main, client = api
    started: list[tuple] = []
    monkeypatch.setattr(main, "_start_build_job",
                        lambda request, kind, worker, args: started.append((kind, args)) or {"job_id": "x"})

    assert client.post("/neighbors", json={}).status_code == 400
    client.post("/neighbors", json={"person_name": "Dana White"})
    assert started[-1] == ("neighbors", ("Dana White",))


def test_registry_failure_never_fails_the_run(contacts_db, monkeypatch):
    """Enrichment is best-effort: a registry that is down costs nothing."""
    import app.discover_engine as de
    importlib.reload(de)

    def boom(*a, **k):
        raise RuntimeError("registry on fire")

    monkeypatch.setattr(de.registry, "co_officers", boom)
    monkeypatch.setattr(de, "find_people", lambda prompt, **kw: type("R", (), {
        "data": {"candidates": [], "notes": ["none"], "interpretation": {}},
        "usage": {}, "validation": {"errors": [], "repairs": [], "warnings": []},
    })())

    out = de.run_discover("superintendents", "Alice Example")
    assert out["found"] is False and out["reason"] == "none"
