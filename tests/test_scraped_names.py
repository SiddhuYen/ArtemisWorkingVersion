"""Tests for the scraped-name validation boundary.

A placeholder that survives this layer reaches the model payload as a person
with a role and a government source_url attached, under a prompt section that
says TREAT THESE AS CONFIRMED. So the important half of this file is not the
rejection cases — it is the acceptance cases, which prove the filter is not
quietly eating real people.

Run: .venv/bin/python -m pytest tests/test_scraped_names.py -q
"""
from __future__ import annotations

import logging

import pytest

from app import scraped_names as sn


# --------------------------------------------------------------- rejections
@pytest.mark.parametrize("raw", [
    "", "   ", "\t\n", None,
])
def test_rejects_empty_and_whitespace(raw):
    assert sn.is_placeholder(raw)
    assert sn.clean_person_name(raw) is None


@pytest.mark.parametrize("raw", [
    "123", "1234", "12-34", "...", "---", "___", "*", "( )", "  .  ", "999999",
])
def test_rejects_numeric_and_punctuation_only(raw):
    assert sn.is_placeholder(raw), f"{raw!r} should be rejected"


@pytest.mark.parametrize("raw", [
    "NOT LISTED", "Not Listed", "not listed", "NOT  LISTED",
    "NONE LISTED", "NOT APPLICABLE", "NOT PROVIDED", "NOT AVAILABLE",
    "NOT ON FILE",
])
def test_rejects_not_variants(raw):
    assert sn.is_placeholder(raw)


@pytest.mark.parametrize("raw", [
    "SAME AS SEC", "SAME AS SEC.", "SAME AS PRES.", "SAME AS PRESIDENT",
    "Same as officers", "SAME AS ABOVE", "SAME",
])
def test_rejects_same_as_backreferences(raw):
    assert sn.is_placeholder(raw)


@pytest.mark.parametrize("raw", [
    "N/A", "n/a", "N / A", "NA", "N A", "NONE", "None", "NON", "NULL", "NIL",
    "UNKNOWN", "VACANT", "TBD", "TBA", "PENDING", "REDACTED", "WITHHELD",
])
def test_rejects_standalone_placeholder_tokens(raw):
    assert sn.is_placeholder(raw)


@pytest.mark.parametrize("raw", [
    "SEE ATTACHED", "See attached", "SEE ABOVE", "SEE BELOW", "ON FILE",
    "TO BE DETERMINED", "TO BE ANNOUNCED",
])
def test_rejects_see_and_deferral_forms(raw):
    assert sn.is_placeholder(raw)


@pytest.mark.parametrize("raw", [
    "AND OTHER", "AND OTHERS", "OTHER", "OTHERS",
])
def test_rejects_and_other_forms(raw):
    assert sn.is_placeholder(raw)


@pytest.mark.parametrize("raw", [
    "NO OFFICERS", "NO OFFICER", "NO DIRECTORS", "NO MEMBERS", "NO AGENT",
])
def test_rejects_no_officers_forms(raw):
    assert sn.is_placeholder(raw)


@pytest.mark.parametrize("raw", [
    "Title", "TYPE", "Name", "Name/Address", "Individual Name", "ROLE",
    "OFFICER", "OFFICERS", "DIRECTOR", "MEMBER", "MANAGER", "ORGANIZER",
    "INCORPORATOR", "PRESIDENT", "SECRETARY", "TREASURER", "AGENT",
    "REGISTERED AGENT", "ADDRESS",
])
def test_rejects_table_headers_and_bare_role_words(raw):
    """These match the <tr><th>role</th><td>name</td> row shape and leak in."""
    assert sn.is_placeholder(raw)


# --------------------------------------------------------------- acceptances
@pytest.mark.parametrize("raw", [
    # Real names captured from live WV and RI filings.
    "CHRISTOPHER MILLER", "CRAIG HETTLINGER", "DR. ROBIN ARORA",
    "L. C. BLACK", "MARY MCCUNE BLACK", "ROBERT T. SMITH JR.",
    "JOSEPH F. SHAFFER, JR.", "J. PATRICK SHAFFER", "SHELDON D. KORLIN",
    "JENNIFER A PAIVA", "EDWARD T. BRODERICK", "G. JOHN GAZERRO, JR. ESQ.",
    "HERCULES ANASTASIADIS", "IRNESA OKANOVIC", "B. Joseph Reddish Iii",
])
def test_accepts_real_names_from_live_filings(raw):
    assert not sn.is_placeholder(raw), f"{raw!r} is a real person and was dropped"
    assert sn.clean_person_name(raw)


@pytest.mark.parametrize("raw", [
    # Surnames that a naive substring denylist for NOT / SEE / NONE would eat.
    "NOTARO", "JAMES NOTARO", "SEELEY", "ANNE SEELEY", "NONESUCH",
    "SEEGER", "PETE SEEGER", "NOTTINGHAM", "MARY NOTTINGHAM",
    "NOLAN", "NOEL", "SEEBER",
])
def test_word_boundaries_protect_real_surnames(raw):
    """A bare `"NOT" in name` test is how a placeholder filter starts eating
    people. These must all survive."""
    assert not sn.is_placeholder(raw), f"{raw!r} was eaten by a substring rule"


@pytest.mark.parametrize("raw", [
    "PDC ENERGY, INC.", "CT CORPORATION SYSTEM", "CORPORATION SERVICE COMPANY",
])
def test_accepts_corporate_officers(raw):
    """A company can genuinely be a General Partner or Member on a filing.

    Distinguishing a company from a warm-path human is a downstream judgment
    (see the human/agent/entity split in scripts/registry_hitrate.py), not a
    parse-boundary one — dropping these here would lose real record data.
    """
    assert not sn.is_placeholder(raw)


def test_all_caps_multi_word_is_deliberately_accepted():
    """Both registries upper-case every name.

    Measured against the 54 human co-officers in the last hit-rate run, an
    all-caps-multi-word rejection rule would have dropped 100% of them.
    """
    assert not sn.is_placeholder("RICHARD LAWLESS")
    assert not sn.is_placeholder("LOWELL PUHLMANN")


# --------------------------------------------------------------- behaviour
def test_normalises_whitespace_and_stray_punctuation():
    assert sn.clean_person_name("  JOHN   SMITH  ") == "JOHN SMITH"
    assert sn.clean_person_name("*JOHN SMITH*") == "JOHN SMITH"


def test_rejected_values_are_dropped_never_substituted():
    """No sentinel, no empty string, no 'Unknown' — the row simply disappears."""
    for junk in ("NOT LISTED", "N/A", "", "   ", "123"):
        assert sn.clean_person_name(junk) is None


def test_rejection_reason_is_populated_for_logging():
    assert sn.rejection_reason("NOT LISTED")
    assert sn.rejection_reason("") == "empty or whitespace only"
    assert sn.rejection_reason("CHRISTOPHER MILLER") is None


def test_rejection_is_logged_as_missing_data(caplog):
    with caplog.at_level(logging.DEBUG, logger="app.scraped_names"):
        assert sn.clean_person_name("SAME AS SEC.", context="WV role=Treasurer") is None
    logged = [r.getMessage() for r in caplog.records]
    assert any("SAME AS SEC" in m for m in logged), f"drop was not logged: {logged}"
    assert any("placeholder phrase" in m for m in logged), "reason not logged"
