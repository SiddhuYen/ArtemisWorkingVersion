"""Parser tests against saved HTML fixtures.

The point of these is failure loudness. A state silently changing its markup
turns a working registry into one that returns zero officers forever, and
nothing downstream notices — a route just quietly gets worse. These assert on
real captured pages so that a markup change breaks a test instead.

Fixtures were captured 2026-08-09 from live pages. Regenerate with
scripts/refresh_fixtures.py when a state legitimately changes its markup, and
read the diff before trusting the new one.

Run: .venv/bin/python -m pytest tests/test_registry_parsers.py -q
"""
from __future__ import annotations

import pathlib

import pytest

from app import registry

FX = pathlib.Path(__file__).parent / "fixtures"


def _fx(name: str) -> str:
    p = FX / name
    if not p.exists():                      # pragma: no cover
        pytest.skip(f"fixture missing: {name}")
    return p.read_text(encoding="utf-8")


# --------------------------------------------------------------- West Virginia
def test_wv_search_extracts_entity_ids():
    rows = registry._wv_parse_search(_fx("wv_search.html"))
    assert rows, "WV name search returned no entities — markup likely changed"
    for org_id, name in rows:
        assert org_id.isdigit(), f"non-numeric org id {org_id!r}"
        assert name.strip()
    assert any("HUNTINGTON" in n.upper() for _, n in rows), \
        f"expected the searched-for entity in results, got {[n for _, n in rows]}"


def test_wv_record_extracts_named_officers():
    officers = registry._wv_parse_record(_fx("wv_record.html"))
    assert officers, "WV officer table produced nothing — markup likely changed"
    roles = {r.lower() for r, _ in officers}
    names = {n for _, n in officers}
    # The header row ("Type" / "Name/Address") matches the <tr><th>..</th><td>
    # shape too and must not survive as an officer.
    assert "type" not in roles
    assert all(n.strip() for n in names)
    # This filing is the one the system prompt cites as the worked example.
    assert any("MILLER" in n.upper() for n in names), \
        f"expected a Miller on this filing, got {sorted(names)}"


def test_wv_record_ignores_pages_without_an_officer_table():
    assert registry._wv_parse_record("<html><body>no officers here</body></html>") == []


@pytest.mark.parametrize("junk", [
    "NOT LISTED", "NONE LISTED", "None", "N/A", "SAME AS PRES.", "SAME AS SEC.",
    "SAME AS OFFICERS", "AND OTHER", "OTHERS", "Vacant", "TBD", "---", "  ",
])
def test_filing_placeholders_are_not_treated_as_people(junk):
    """WV filings put literal boilerplate where a name belongs.

    Emitting one hands the model a fabricated human tagged as registry-attested
    evidence, which is the exact failure the named-on-the-filing rule exists to
    prevent — so it has to be caught here too.
    """
    assert registry._is_placeholder_name(junk), f"{junk!r} would be sent as a person"


@pytest.mark.parametrize("real", [
    "CHRISTOPHER MILLER", "L. C. BLACK", "MARY MCCUNE BLACK", "DR. ROBIN ARORA",
    "ROBERT T. SMITH JR.", "Jennifer A Paiva", "Ed Ng",
])
def test_real_names_survive_the_placeholder_filter(real):
    assert not registry._is_placeholder_name(real), f"{real!r} was wrongly dropped"


def test_wv_parser_emits_no_placeholder_names_on_the_fixture():
    for _role, name in registry._wv_parse_record(_fx("wv_record.html")):
        assert not registry._is_placeholder_name(name)


# --------------------------------------------------------------- Rhode Island
def test_ri_search_extracts_fein_ids():
    rows = registry._RI_RESULT_ROW.findall(_fx("ri_search.html"))
    assert rows, "RI search results produced no CorpSummary links — flow changed"
    for fein, _label in rows:
        assert fein.isdigit()


def test_ri_record_extracts_officers_with_titles():
    officers = registry._ri_parse_record(_fx("ri_record.html"))
    assert officers, "RI officer grid produced nothing — markup likely changed"
    roles = [r for r, _ in officers]
    assert "Title" not in roles, "grid header leaked into officer rows"
    assert any(r.upper() == "PRESIDENT" for r in roles), \
        f"expected a titled officer, got {roles}"


def test_ri_record_labels_the_registered_agent_distinctly():
    """A registered agent is a service provider, not a warm path.

    It must come back tagged so a caller can tell it apart from a real officer;
    silently mixing it into the officer list is how a law firm becomes a
    'co-officer'.
    """
    officers = registry._ri_parse_record(_fx("ri_record.html"))
    agents = [n for r, n in officers if r == "Registered Agent"]
    assert len(agents) == 1, f"expected exactly one tagged agent, got {agents}"
    assert agents[0].strip()


def test_ri_aspnet_form_roundtrips_viewstate_and_drops_sitesearch_junk():
    form = registry._aspnet_form(_fx("ri_search.html"))
    assert "__VIEWSTATE" in form, "viewstate not recovered — POST would 500"
    for junk in registry._RI_DROP:
        assert junk not in form, f"site-search field {junk!r} would break the postback"


# --------------------------------------------------------- shared invariants
@pytest.mark.parametrize("state", sorted(registry.REGISTRIES))
def test_every_registry_declares_a_record_url_placeholder(state):
    assert "{org_id}" in registry.REGISTRIES[state].record_url


@pytest.mark.parametrize("state", sorted(registry.REGISTRIES))
def test_every_registry_documents_its_access_posture(state):
    assert registry.REGISTRIES[state].access_note.strip(), \
        "each state must record how it is reached and what robots.txt said"


def test_states_we_deliberately_skipped_are_not_also_implemented():
    overlap = set(registry.UNAVAILABLE) & set(registry.REGISTRIES)
    assert not overlap, f"{overlap} listed as both unavailable and implemented"


def test_state_from_text_prefers_the_longer_name():
    assert registry.state_from_text("Huntington, West Virginia") == "WV"
    assert registry.state_from_text("Providence, Rhode Island") == "RI"


def test_co_officers_is_inert_for_unimplemented_states():
    """A state we cannot reach must cost zero requests, not a failed fetch."""
    assert registry.co_officers("Someone", "a company in Delaware") == []
    assert registry.co_officers("Someone", "a company in Texas") == []


# --------------------------------------------------------- politeness/budget
def test_fetcher_enforces_the_per_host_cap():
    f = registry.Fetcher(client=None, cap=3, delay_s=0)  # type: ignore[arg-type]
    for _ in range(3):
        f._gate("example.gov")
    with pytest.raises(registry.HostBudgetExceeded):
        f._gate("example.gov")


def test_fetcher_counts_hosts_independently():
    f = registry.Fetcher(client=None, cap=2, delay_s=0)  # type: ignore[arg-type]
    f._gate("a.gov"); f._gate("a.gov"); f._gate("b.gov")
    assert f.counts == {"a.gov": 2, "b.gov": 1}


def test_user_agent_is_honest_and_contactable():
    ua = registry._UA
    assert "Artemis" in ua
    assert "http" in ua and "contact" in ua
    for spoof in ("Mozilla", "Chrome", "Safari", "AppleWebKit"):
        assert spoof not in ua, f"user agent still spoofs {spoof}"
