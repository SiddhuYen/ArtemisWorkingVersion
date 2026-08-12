"""Tests for organisation-name extraction from an operator's context string.

This was the single largest source of missed co-officers: in the 20-subject
hit-rate run, every miss traced to here rather than to a parser or a missing
record. Each case below is a real context string built from a real filing.

Run: .venv/bin/python -m pytest tests/test_org_candidates.py -q
"""
from __future__ import annotations

import pytest

from app.registry import org_candidates

WV = "in Charleston, West Virginia"
RI = "in Providence, Rhode Island"


@pytest.mark.parametrize("context,expected", [
    # --- the characters that used to end a span early ----------------------
    (f"Director of Allied & Behavioral Healthcare Inc {WV}",
     "Allied & Behavioral Healthcare Inc"),
    (f"Member of Coastal + Captured LLC {WV}",
     "Coastal + Captured LLC"),
    (f'Member of Capital "C" Land Services, LLC {WV}',
     'Capital "C" Land Services, LLC'),
    (f"Incorporator of Central & Russell, Inc. {WV}",
     "Central & Russell, Inc."),
    (f"Incorporator of Community & Economic Development Consultants, Inc. {WV}",
     "Community & Economic Development Consultants, Inc."),
    (f"Director of Atlantic Power & Light Co. {WV}",
     "Atlantic Power & Light Co."),
    # digits inside the name
    (f"General Partner of Eastern 1984 Limited Partnership {WV}",
     "Eastern 1984 Limited Partnership"),
    (f"President of Community 2000 Education Foundation {RI}",
     "Community 2000 Education Foundation"),
    # single-token name, only valid because it followed a preposition
    (f"Treasurer of Northern {RI}", "Northern"),
    (f"President of ProMaxim, Inc. {RI}", "ProMaxim, Inc."),
])
def test_recovers_names_the_old_extractor_truncated(context, expected):
    got = org_candidates(context)
    assert got and got[0] == expected, f"got {got}"


@pytest.mark.parametrize("context,expected", [
    (f"Manager of Advanced Acoustic Concepts, LLC {WV}",
     "Advanced Acoustic Concepts, LLC"),
    (f"co-organizer of Huntington Addiction Wellness Center in Huntington, West Virginia",
     "Huntington Addiction Wellness Center"),
    ("CEO of Dutch Miller Auto Group, Huntington, WV", "Dutch Miller Auto Group"),
    (f"owner of Gilbane Building Company, Providence, Rhode Island",
     "Gilbane Building Company"),
])
def test_does_not_regress_names_that_already_worked(context, expected):
    got = org_candidates(context)
    assert got and got[0] == expected, f"got {got}"


def test_a_company_starting_with_a_role_word_survives():
    """The title strip must be anchored on a following preposition.

    "General Partner of X" -> X, but "General Motors" is the company.
    """
    assert org_candidates("engineer at General Motors in Detroit, Michigan")[0] \
        == "General Motors"
    assert org_candidates("Chairman of Manager Holdings Inc, Dallas, Texas")[0] \
        == "Manager Holdings Inc"


@pytest.mark.parametrize("context", [
    f"Director of Allied Inc {WV}",
    f"President of Acme {RI}",
    "Member of Something LLC in Columbus, Ohio",
])
def test_the_state_name_is_never_searched_as_a_company(context):
    """It was, for 20 of 20 subjects — about half of all registry traffic."""
    for cand in org_candidates(context):
        assert cand.lower() not in {"west virginia", "rhode island", "ohio"}
        assert cand.upper() not in {"WV", "RI", "OH"}


def test_the_city_is_not_searched_as_a_company():
    """A span followed by ", <state>" is a location."""
    got = org_candidates(f"Director of Allied Inc {WV}")
    assert "Charleston" not in got
    assert got == ["Allied Inc"]


@pytest.mark.parametrize("context", [
    "President in Charleston, West Virginia",
    "Manager",
    "the owner",
    "",
])
def test_bare_titles_and_empty_input_yield_nothing(context):
    assert org_candidates(context) == []


def test_a_stray_capitalised_word_is_not_searched():
    """Single tokens only count as the object of a preposition."""
    assert "Tuesday" not in org_candidates("met on Tuesday in Charleston, West Virginia")


def test_respects_the_candidate_limit():
    ctx = ("Director of Alpha Corp and Beta LLC and Gamma Inc and Delta Co "
           "in Charleston, West Virginia")
    assert len(org_candidates(ctx, limit=2)) <= 2
