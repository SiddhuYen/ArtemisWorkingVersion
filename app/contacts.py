"""The operator's own imported network.

A LinkedIn connection is a different kind of evidence from anything the web
search finds. Nothing public says these two people know each other — the proof
is that the operator exported the file. So these rows are only ever usable as
first-degree ties *for the person who uploaded them*, which is why every read
here is scoped by `owner_norm` and why routing only offers them when the route
starts at that same person.

Claude reaches this through a tool rather than a prompt dump. A LinkedIn export
runs to thousands of rows; pasting them into every request would cost more than
the search does and bury the actual question. A tool lets it look up the handful
of names it has a reason to care about.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from collections import Counter
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


def summarize(db: Session, owner_name: str, top: int = 12) -> dict[str, Any]:
    """The shape of a network, rather than a slice of it.

    Search alone gives no way to ask "what is in here", so a caller whose
    guesses miss has nothing to do but guess differently — in practice, probing
    single letters to enumerate the list. That is harmless at three contacts and
    actively misleading at three thousand, where "a" matches most of the file
    and comes back truncated to `limit` with no indication that it was cut.

    Counts are honest where a capped list is not: they say what is actually
    there, so the decision to stop looking can be made on one call.
    """
    rows = list_for_owner(db, owner_name)
    companies = Counter(
        c.company.strip() for c in rows if c.company and c.company.strip()
    )
    titles = Counter(c.title.strip() for c in rows if c.title and c.title.strip())
    return {
        "count": len(rows),
        "companies": companies.most_common(top),
        "titles": titles.most_common(top),
        "distinct_companies": len(companies),
    }


def search(db: Session, owner_name: str, query: str, limit: int = 25) -> list[dict[str, Any]]:
    """Substring search over one owner's contacts, by name, company or title.

    Deliberately dumb: the caller is Claude, which will try several phrasings on
    its own if the first returns nothing. Ranking puts company/title matches
    after name matches so a query like "Sequoia" surfaces people at Sequoia
    rather than anyone whose name happens to contain it.
    """
    q = norm_name(query)
    if not q:
        return []
    rows = list_for_owner(db, owner_name)
    scored: list[tuple[int, Contact]] = []
    for c in rows:
        name = c.norm_name
        company = norm_name(c.company or "")
        title = norm_name(c.title or "")
        if q in name:
            scored.append((0 if name.startswith(q) else 1, c))
        elif q in company:
            scored.append((2, c))
        elif q in title:
            scored.append((3, c))
    scored.sort(key=lambda pair: (pair[0], pair[1].canonical_name))
    return [
        {
            "name": c.canonical_name,
            "title": c.title or "",
            "company": c.company or "",
            "connected_on": c.connected_on or "",
        }
        for _, c in scored[:limit]
    ]
