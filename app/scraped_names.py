"""Validation boundary for person names lifted out of scraped registry pages.

Registry pages routinely put boilerplate where a name belongs — "NOT LISTED",
"SAME AS SEC.", "N/A", "AND OTHER". Downstream those become
`co_officers[].name`, which route_engine drops into the model payload under a
prompt section headed TREAT THESE AS CONFIRMED (prompt.py:52). So a placeholder
that survives is not a cosmetic defect: it is a fabricated human arriving with
attested-evidence status, a named role, and a source_url pointing at a real
government record. That is the single worst output this pipeline can produce.

This module is the one place scraped text becomes a name. Nothing scraped
should reach a payload without passing through `clean_person_name`.

POLICY
  * A rejected value is DROPPED. It is never substituted with a placeholder of
    our own, never emitted as an empty string, and never carried forward with
    its role or source_url attached.
  * Rejection is logged at DEBUG with the reason, so a state that starts
    emitting a new placeholder form shows up as a spike in dropped names
    rather than as silence.

DELIBERATELY NOT REJECTED
  * ALL-CAPS multi-word strings. Both implemented registries return every name
    upper-cased ("CHRISTOPHER MILLER", "JENNIFER A PAIVA"); measured against
    the 54 human co-officers in the last hit-rate run, an all-caps rule would
    reject 100% of them.
  * Company-shaped names ("PDC ENERGY, INC."). A corporate entity genuinely can
    be a General Partner or Member on a filing, so dropping those loses real
    record data. Telling a company apart from a warm-path human is a
    downstream judgment, not a parse-boundary one — see the human/agent/entity
    split in scripts/registry_hitrate.py.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# Whole-string values that are never a person, compared after normalisation
# (collapse whitespace, strip surrounding punctuation, upper-case).
_DENY_EXACT = frozenset({
    "N/A", "NA", "N A", "NONE", "NON", "NULL", "NIL", "UNKNOWN", "UNK",
    "VACANT", "TBD", "TBA", "PENDING", "OTHER", "OTHERS", "AND OTHER",
    "AND OTHERS", "SAME", "SEE ATTACHED", "SEE ABOVE", "SEE BELOW",
    "ON FILE", "WITHHELD", "REDACTED", "NOT APPLICABLE", "NOT PROVIDED",
    "NOT AVAILABLE", "NOT LISTED", "NONE LISTED", "NOT ON FILE",
    "NO OFFICER", "NO OFFICERS", "NO DIRECTOR", "NO DIRECTORS",
    "NO MEMBERS", "NO AGENT", "TO BE DETERMINED", "TO BE ANNOUNCED",
    # Column headers that match the row shape and leak through a table parse.
    "NAME", "NAME/ADDRESS", "INDIVIDUAL NAME", "TITLE", "TYPE", "ROLE",
    "OFFICER", "OFFICERS", "DIRECTOR", "DIRECTORS", "MEMBER", "MEMBERS",
    "MANAGER", "MANAGERS", "ORGANIZER", "ORGANIZERS", "INCORPORATOR",
    "INCORPORATORS", "PRESIDENT", "SECRETARY", "TREASURER", "AGENT",
    "REGISTERED AGENT", "ADDRESS",
})

# Phrases that disqualify a value wherever they appear. Word-boundary anchored
# on purpose: a bare substring test would reject the real surnames NOTARO,
# SEELEY and NONESUCH, which is how a placeholder filter starts eating people.
_DENY_PHRASE = re.compile(
    r"\b(?:"
    r"not\s+listed|none\s+listed|not\s+applicable|not\s+provided|"
    r"not\s+available|not\s+on\s+file|no\s+officers?|no\s+directors?|"
    r"same\s+as|see\s+attached|see\s+above|see\s+below|on\s+file|"
    r"to\s+be\s+determined|to\s+be\s+announced|n/a"
    r")\b", re.I)

# Standalone disqualifying words, again word-boundary anchored.
_DENY_WORD = re.compile(r"\b(?:none|null|unknown|vacant|tbd|tba|redacted|withheld)\b", re.I)

# Two consecutive letters somewhere: rejects "123", "---", ".", "1234 MAIN ST"
# style fragments that are addresses rather than names.
_HAS_LETTERS = re.compile(r"[A-Za-z]{2}")

_MIN_LEN = 3


def _normalise(raw: str) -> str:
    """Collapse whitespace and strip decorative punctuation for comparison."""
    s = re.sub(r"\s+", " ", (raw or "")).strip()
    return s.strip(" .,;:-_*()[]{}\"'").strip()


def rejection_reason(raw: str) -> str | None:
    """None when `raw` is usable as a person's name, else why it was rejected."""
    if raw is None or not str(raw).strip():
        return "empty or whitespace only"

    s = _normalise(str(raw))
    if not s:
        return "punctuation only"
    if len(s) < _MIN_LEN:
        return f"shorter than {_MIN_LEN} characters"
    if not _HAS_LETTERS.search(s):
        return "no alphabetic content (numeric or punctuation only)"

    upper = s.upper()
    if upper in _DENY_EXACT:
        return f"known placeholder / column header ({s!r})"
    if _DENY_PHRASE.search(s):
        return f"contains placeholder phrase ({s!r})"
    if _DENY_WORD.search(s):
        return f"contains placeholder word ({s!r})"
    # "SAME AS PRES." / "SAME AS SEC." survive the phrase test only if the
    # trailing token was stripped; anchor the prefix too.
    if upper.startswith("SAME AS") or upper.startswith("SEE "):
        return f"back-reference rather than a name ({s!r})"
    return None


def clean_person_name(raw: str, *, context: str = "") -> str | None:
    """The usable name, or None if this value must be dropped.

    `context` is only used to make the debug log actionable (which state, which
    entity) — it never affects the decision.
    """
    reason = rejection_reason(raw)
    if reason is None:
        return _normalise(str(raw))
    log.debug("dropped scraped name %r%s: %s", raw,
              f" [{context}]" if context else "", reason)
    return None


def is_placeholder(raw: str) -> bool:
    """True when `raw` must not be treated as a person's name."""
    return rejection_reason(raw) is not None
