"""Authorization tests: owner scoping must come from the session, never the caller.

The bug these cover: `owner_name` was a query/body/form parameter and
`X-Graph-Id` was a header, and both selected rows. Any holder of the shared
token could read or delete another operator's imported contacts — real names,
employers, emails and LinkedIn URLs for people who never used this app — by
changing a string.

The important assertions here are the negative ones: that a request *cannot
express* another identity, not merely that it is ignored.

Run: .venv/bin/python -m pytest tests/test_authorization.py -q
"""
from __future__ import annotations

import importlib
import os

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

TOKEN_A = "tok_alice_aaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TOKEN_B = "tok_bob_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


@pytest.fixture()
def app_two_operators(tmp_path, monkeypatch):
    """App with two configured operators and an isolated boards DB."""
    monkeypatch.setenv("ARTEMIS_OPERATORS",
                       f"alice:Alice Example:{TOKEN_A},bob:Bob Example:{TOKEN_B}")
    monkeypatch.setenv("ARTEMIS_BOARDS_DB_URL", f"sqlite:///{tmp_path}/boards.db")
    monkeypatch.delenv("ARTEMIS_ACCESS_TOKEN", raising=False)

    import app.config, app.db, app.models, app.auth, app.contacts, app.main
    for mod in (app.config, app.db, app.models, app.auth, app.contacts, app.main):
        importlib.reload(mod)
    app.main.init_boards_db()
    return app.main


def _client(mod, token=None):
    c = TestClient(mod.app)
    if token:
        c.headers.update({"Authorization": f"Bearer {token}"})
    return c


def _seed(mod, token, names):
    c = _client(mod, token)
    r = c.post("/network/contacts/import",
               json={"contacts": [{"name": n, "company": "Acme"} for n in names]})
    assert r.status_code == 200, r.text
    return r.json()


# ----------------------------------------------------- the vulnerability itself
def test_contacts_are_isolated_between_operators(app_two_operators):
    m = app_two_operators
    _seed(m, TOKEN_A, ["Alice Contact One", "Alice Contact Two"])
    _seed(m, TOKEN_B, ["Bob Contact One"])

    a = _client(m, TOKEN_A).get("/network/profiles").json()
    b = _client(m, TOKEN_B).get("/network/profiles").json()

    assert {c["canonical_name"] for c in a} == {"Alice Contact One", "Alice Contact Two"}
    assert {c["canonical_name"] for c in b} == {"Bob Contact One"}


def test_owner_name_query_param_cannot_reach_another_operator(app_two_operators):
    """The original exploit: ?owner_name=<someone else>."""
    m = app_two_operators
    _seed(m, TOKEN_A, ["Alice Only"])
    _seed(m, TOKEN_B, ["Bob Only"])

    bob = _client(m, TOKEN_B)
    for probe in ("alice", "Alice Example", "ALICE", "alice example"):
        got = bob.get("/network/profiles", params={"owner_name": probe}).json()
        assert {c["canonical_name"] for c in got} == {"Bob Only"}, \
            f"owner_name={probe!r} changed the result set"


def test_owner_name_is_removed_from_the_schema_not_merely_ignored(app_two_operators):
    """A request must not be able to *express* another identity."""
    schema = _client(m := app_two_operators, TOKEN_A).get("/openapi.json").json()
    offenders = []
    for path, ops in schema["paths"].items():
        for method, op in ops.items():
            for param in op.get("parameters", []) or []:
                if param.get("name") in {"owner_name", "X-Graph-Id", "owner_id"}:
                    offenders.append(f"{method.upper()} {path} -> {param['name']}")
    assert not offenders, f"caller-supplied scoping still in the API surface: {offenders}"


def test_delete_cannot_target_another_operator(app_two_operators):
    m = app_two_operators
    _seed(m, TOKEN_A, ["Alice Keeps This"])
    _seed(m, TOKEN_B, ["Bob Deletes His Own"])

    deleted = _client(m, TOKEN_B).delete(
        "/network/profiles", params={"owner_name": "alice"}).json()
    assert deleted["deleted"] == 1, "deleted the wrong operator's rows"

    still_there = _client(m, TOKEN_A).get("/network/profiles").json()
    assert {c["canonical_name"] for c in still_there} == {"Alice Keeps This"}


def test_boards_are_isolated_and_x_graph_id_is_inert(app_two_operators):
    m = app_two_operators
    made = _client(m, TOKEN_A).post("/boards", json={"name": "Alice board"}).json()

    bob = _client(m, TOKEN_B)
    assert bob.get("/boards").json() == []
    # The old header must not resurrect cross-operator access.
    spoofed = bob.get("/boards", headers={"X-Graph-Id": "alice"}).json()
    assert spoofed == []
    assert bob.get(f"/boards/{made['id']}").status_code == 404


def test_upload_attributes_to_the_session_not_a_form_field(app_two_operators):
    m = app_two_operators
    csv = b"First Name,Last Name,Company\nJane,Doe,Acme\n"
    r = _client(m, TOKEN_B).post(
        "/network/upload",
        files={"file": ("Connections.csv", csv, "text/csv")},
        data={"owner_name": "alice"},          # attacker-supplied; must not bind
    )
    assert r.status_code == 200, r.text
    assert _client(m, TOKEN_A).get("/network/profiles").json() == []
    assert [c["canonical_name"]
            for c in _client(m, TOKEN_B).get("/network/profiles").json()] == ["Jane Doe"]


# --------------------------------------------------------------- authentication
def test_unauthenticated_requests_are_rejected(app_two_operators):
    m = app_two_operators
    assert TestClient(m.app).get("/network/profiles").status_code == 401
    assert TestClient(m.app).get("/boards").status_code == 401


def test_a_bad_token_is_rejected(app_two_operators):
    m = app_two_operators
    assert _client(m, "tok_not_real").get("/network/profiles").status_code == 401


def test_login_issues_a_distinct_cookie_per_operator(app_two_operators):
    m = app_two_operators
    ca, cb = TestClient(m.app), TestClient(m.app)
    ra = ca.post("/login", json={"token": TOKEN_A})
    rb = cb.post("/login", json={"token": TOKEN_B})
    assert ra.json()["operator"] == "alice"
    assert rb.json()["operator"] == "bob"

    import app.config as cfg
    va = ra.cookies.get(cfg.SESSION_COOKIE)
    vb = rb.cookies.get(cfg.SESSION_COOKIE)
    assert va and vb and va != vb, \
        "session cookies are identical — the cookie carries no identity"


def test_session_cookie_alone_resolves_the_right_operator(app_two_operators):
    m = app_two_operators
    _seed(m, TOKEN_A, ["Alice Cookie Contact"])
    c = TestClient(m.app)
    c.post("/login", json={"token": TOKEN_A})
    got = c.get("/network/profiles").json()          # cookie only, no bearer
    assert [x["canonical_name"] for x in got] == ["Alice Cookie Contact"]


def test_session_value_is_per_token():
    import app.auth as a
    assert a.session_value(TOKEN_A) != a.session_value(TOKEN_B)


# ------------------------------------------------------------ query-layer scope
def test_contacts_helpers_refuse_an_empty_owner():
    """An owner-scoped query with no owner is an unfiltered table read."""
    import app.contacts as c
    for fn in (c.list_for_owner, c.delete_for_owner):
        with pytest.raises(ValueError):
            fn(None, "")
    with pytest.raises(ValueError):
        c.ingest(None, "", [{"name": "X"}])


def test_open_mode_uses_a_constant_identity(tmp_path, monkeypatch):
    """With no token configured the app is open, but identity is still fixed."""
    monkeypatch.delenv("ARTEMIS_OPERATORS", raising=False)
    monkeypatch.delenv("ARTEMIS_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("ARTEMIS_BOARDS_DB_URL", f"sqlite:///{tmp_path}/open.db")
    import app.config, app.db, app.auth, app.main
    for mod in (app.config, app.db, app.auth, app.main):
        importlib.reload(mod)
    app.main.init_boards_db()

    c = TestClient(app.main.app)
    assert c.get("/network/profiles").status_code == 200
    # even here the caller cannot choose whose rows they get
    assert c.get("/network/profiles", params={"owner_name": "somebody"}).json() == []
