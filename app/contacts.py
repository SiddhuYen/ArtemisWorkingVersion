"""The operator's own imported network.

A LinkedIn connection is a different kind of evidence from anything the web
search finds. Nothing public says these two people know each other — the proof
is that the operator exported the file. So these rows are only ever usable as
first-degree ties *for the person who uploaded them*, which is why every read
here is scoped by `owner_norm` and why routing only offers them when the route
starts at that same person.

A LinkedIn export runs to thousands of rows, which cannot all go into a prompt:
it would cost more than the search does and bury the actual question. So the
list is narrowed here, locally, and only the shortlist travels — see
`top_candidates`. Ranking a few thousand rows is a database job, not a reasoning
one, and doing it up front keeps a route to a single request.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from typing import Any, Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import Contact

# LinkedIn's export has a preamble ("Notes:", a blurb, blank lines) before the
# real header row. The column names have also moved around between exports, so
# match on a normalised header rather than a fixed position.
_HEADER_ALIASES = {
    "first name": "first_name",
    "last name": "last_name",
    "full name": "full_name",
    "name": "full_name",
    "email address": "email",
    "emailaddress": "email",
    "company": "company",
    "position": "title",
    "title": "title",
    "url": "linkedin_url",
    "profile url": "linkedin_url",
    "connected on": "connected_on",
}

_WS = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)


def norm_name(value: str) -> str:
    """Casefold, strip accents and punctuation, collapse whitespace."""
    decomposed = unicodedata.normalize("NFKD", value or "")
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _WS.sub(" ", _NON_WORD.sub(" ", stripped.casefold())).strip()


def parse_linkedin_csv(raw: bytes | str) -> list[dict[str, str]]:
    """Rows out of a LinkedIn Connections.csv, tolerant of export drift.

    Skips the notes preamble by scanning for the first line that looks like a
    header, so a file with — or without — it parses the same way.
    """
    text = raw.decode("utf-8-sig", errors="replace") if isinstance(raw, bytes) else raw
    lines = text.splitlines()

    start = 0
    for i, line in enumerate(lines[:20]):
        cells = [norm_name(c) for c in next(csv.reader([line]), [])]
        if any(c in _HEADER_ALIASES for c in cells) and len(cells) >= 2:
            start = i
            break

    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])))
    out: list[dict[str, str]] = []
    for row in reader:
        mapped: dict[str, str] = {}
        for key, value in (row or {}).items():
            if key is None:
                continue
            field = _HEADER_ALIASES.get(norm_name(key))
            if field and value:
                mapped[field] = str(value).strip()
        name = mapped.get("full_name") or " ".join(
            p for p in (mapped.get("first_name"), mapped.get("last_name")) if p
        ).strip()
        if not name:
            continue
        out.append({
            "name": name,
            "company": mapped.get("company", ""),
            "title": mapped.get("title", ""),
            "email": mapped.get("email", ""),
            "linkedin_url": mapped.get("linkedin_url", ""),
            "connected_on": mapped.get("connected_on", ""),
        })
    return out


def ingest(db: Session, owner_name: str, rows: Iterable[dict[str, Any]],
           source: str = "linkedin_csv") -> dict[str, int]:
    """Upsert rows for one owner. Returns {created, updated, skipped}."""
    owner = norm_name(owner_name)
    if not owner:
        raise ValueError("owner_name is required — contacts are only usable as "
                         "first-degree ties for the person who uploaded them")

    existing = {
        c.norm_name: c
        for c in db.execute(select(Contact).where(Contact.owner_norm == owner)).scalars()
    }
    created = updated = skipped = 0
    seen: set[str] = set()

    for row in rows:
        name = str(row.get("name") or "").strip()
        key = norm_name(name)
        if not key or key in seen:
            skipped += 1
            continue
        seen.add(key)
        fields = {
            "company": (row.get("company") or "").strip() or None,
            "title": (row.get("title") or "").strip() or None,
            "email": (row.get("email") or "").strip() or None,
            "linkedin_url": (row.get("linkedin_url") or "").strip() or None,
            "notes": (row.get("notes") or "").strip() or None,
            "connected_on": (row.get("connected_on") or "").strip() or None,
        }
        current = existing.get(key)
        if current is None:
            db.add(Contact(owner_norm=owner, canonical_name=name, norm_name=key,
                           source=source, **fields))
            created += 1
        else:
            # Re-import must not blank a field the newer export omits.
            changed = False
            for attr, value in fields.items():
                if value and getattr(current, attr) != value:
                    setattr(current, attr, value)
                    changed = True
            if current.canonical_name != name:
                current.canonical_name = name
                changed = True
            updated += changed
            skipped += not changed
    db.commit()
    return {"created": created, "updated": updated, "skipped": skipped}


def list_for_owner(db: Session, owner_name: str | None) -> list[Contact]:
    stmt = select(Contact)
    if owner_name:
        stmt = stmt.where(Contact.owner_norm == norm_name(owner_name))
    return list(db.execute(stmt.order_by(Contact.canonical_name)).scalars())


def count_for_owner(db: Session, owner_name: str) -> int:
    return len(list_for_owner(db, owner_name))


def delete_for_owner(db: Session, owner_name: str | None) -> int:
    stmt = delete(Contact)
    if owner_name:
        stmt = stmt.where(Contact.owner_norm == norm_name(owner_name))
    result = db.execute(stmt)
    db.commit()
    return int(result.rowcount or 0)


def to_profile_dict(c: Contact) -> dict[str, Any]:
    """The shape the board UI already expects from /network/profiles."""
    return {
        "id": c.id,
        "canonical_name": c.canonical_name,
        "titles": [c.title] if c.title else [],
        "companies": [c.company] if c.company else [],
        "email": c.email or "",
        "notes": c.notes or "",
        "connected_on": c.connected_on or "",
        "linkedin_url": c.linkedin_url or "",
    }


# Words that carry no signal when matching a contact against a target: legal
# suffixes, and the connective tissue of a job description.
_STOPWORDS = {
    "the", "and", "of", "at", "for", "inc", "llc", "ltd", "corp", "corporation",
    "company", "co", "group", "holdings", "plc", "sa", "ag", "gmbh", "limited",
    "partners", "capital", "ventures", "global", "international", "technologies",
    "technology", "solutions", "services", "systems", "labs",
}

# Seniority ladder. Someone junior at the right company usually cannot make the
# introduction; someone senior often can, even a step further out. Longer
# phrases are checked first so "managing partner" does not score as "partner".
_SENIORITY = (
    (("chief executive", "ceo", "founder", "co-founder", "cofounder", "chairman",
      "chairwoman", "chair", "president", "managing partner", "general partner",
      "managing director", "cto", "cfo", "coo", "cmo", "chief"), 6),
    (("partner", "vice president", "vp", "svp", "evp", "head of", "director",
      "principal", "board member", "advisor"), 4),
    (("lead", "senior", "staff", "manager", "founding"), 2),
)


def _terms(text: str) -> set[str]:
    return {t for t in norm_name(text).split() if len(t) >= 3 and t not in _STOPWORDS}


def _seniority(title: str) -> int:
    t = norm_name(title)
    for phrases, score in _SENIORITY:
        if any(p in t for p in phrases):
            return score
    return 0


def top_candidates(db: Session, owner_name: str, target: str,
                   limit: int = 10) -> list[dict[str, Any]]:
    """The connections most likely to be able to reach `target`.

    Ranking is deliberately crude and local: it only has to produce a shortlist
    worth reasoning about, not to pick the answer. Two signals are available
    from a LinkedIn export and both matter:

      * overlap with the target's world — their employer and the vocabulary of
        their industry, matched against the contact's company and title
      * seniority — a junior person at the right company usually cannot make an
        introduction, while a senior one often can from a step further out

    Everyone is returned when the network is smaller than `limit`; there is
    nothing to choose between and dropping people would only hide options.
    """
    rows = list_for_owner(db, owner_name)
    if len(rows) <= limit:
        ranked = rows
    else:
        want = _terms(target)
        scored: list[tuple[int, str, Contact]] = []
        for c in rows:
            company_hits = len(want & _terms(c.company or ""))
            title_hits = len(want & _terms(c.title or ""))
            score = company_hits * 10 + title_hits * 4 + _seniority(c.title or "")
            scored.append((-score, c.canonical_name, c))
        scored.sort()
        ranked = [c for _, _, c in scored[:limit]]

    return [
        {
            "name": c.canonical_name,
            "title": c.title or "",
            "company": c.company or "",
            "connected_on": c.connected_on or "",
        }
        for c in ranked
    ]
