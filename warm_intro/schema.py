"""Shape checking and invariant enforcement for the returned object.

The model is asked for a contract. This module makes the contract true before a
caller acts on it, because the caller is a program that will spend real social
capital with a real person.

Two failure classes are handled differently:

* Structural violations (missing key, wrong type) are recorded as `errors`. The
  object is unsafe to act on; `strict=True` raises.
* Rule violations that are mechanically derivable — rating vs. weakest hop,
  dropped vs. null path — are repaired in place and recorded, because the
  derivation is unambiguous and re-prompting for it would be wasteful.
"""

from __future__ import annotations

import re
from typing import Any

STRENGTHS = ("weak", "moderate", "strong")
_STRENGTH_RANK = {s: i for i, s in enumerate(STRENGTHS)}
RATINGS = ("strong", "moderate", "weak", "dropped")

TOP_LEVEL_KEYS = (
    "target",
    "target_context",
    "path",
    "hops",
    "rating",
    "weak_points",
    "first_action",
    "clusters_considered",
    "searches_used",
)

MAX_INTERMEDIARIES = 2
MAX_PATH_NODES = MAX_INTERMEDIARIES + 2  # start + intermediaries + target

# The SCOPE LIMIT section forbids these. We flag rather than strip: silently
# editing a field would hide a prompt regression from whoever is on call.
_PII_PATTERNS = {
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    "phone": re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)"),
    "street_address": re.compile(
        r"\b\d{1,5}\s+[\w.\s]{2,40}\b(?:street|st|avenue|ave|road|rd|drive|dr|lane|ln|"
        r"boulevard|blvd|court|ct|way|place|pl)\b\.?",
        re.IGNORECASE,
    ),
}


class SchemaError(ValueError):
    """The object does not conform and cannot be mechanically repaired."""


class ValidationReport:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.repairs: list[str] = []
        self.warnings: list[str] = []

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, list[str]]:
        return {"errors": self.errors, "repairs": self.repairs, "warnings": self.warnings}


def _is_str(v: Any) -> bool:
    return isinstance(v, str)


def weakest(strengths: list[str]) -> str:
    known = [s for s in strengths if s in _STRENGTH_RANK]
    if not known:
        return "weak"
    return min(known, key=lambda s: _STRENGTH_RANK[s])


def downgrade(current: str, floor: str = "weak") -> str:
    """Return the weaker of `current` and `floor`."""
    a = _STRENGTH_RANK.get(current, 0)
    b = _STRENGTH_RANK.get(floor, 0)
    return STRENGTHS[min(a, b)]


def drop_path(data: dict[str, Any], reason: str) -> None:
    """Collapse the object to a dropped result, preserving what was learned.

    The rejected path moves into `clusters_considered` so the information is not
    lost, and `first_action` is blanked — a caller reading only that field must
    not be handed a contact instruction for a path we just invalidated.
    """
    path = data.get("path")
    if isinstance(path, list) and path:
        names = " -> ".join(
            str(n.get("name", "?")) for n in path if isinstance(n, dict)
        )
        data.setdefault("clusters_considered", [])
        if isinstance(data["clusters_considered"], list):
            data["clusters_considered"].append(
                {"cluster": names, "why_dropped": reason}
            )

    data["path"] = None
    data["hops"] = []
    data["rating"] = "dropped"
    data["first_action"] = {"who": "", "contacts": "", "ask": ""}

    wp = data.get("weak_points")
    if not isinstance(wp, list):
        wp = []
    if reason not in wp:
        wp.append(reason)
    data["weak_points"] = wp


def validate_and_repair(data: dict[str, Any], *, strict: bool = False) -> ValidationReport:
    """Check structure, then enforce the rating rules. Mutates `data`."""
    r = ValidationReport()

    for key in TOP_LEVEL_KEYS:
        if key not in data:
            r.errors.append(f"missing required key: {key}")

    for key in ("target", "target_context", "rating"):
        if key in data and not _is_str(data[key]):
            r.errors.append(f"{key} must be a string, got {type(data.get(key)).__name__}")

    # --- normalise container types ------------------------------------------
    for key, default in (("hops", []), ("weak_points", []), ("clusters_considered", [])):
        if not isinstance(data.get(key), list):
            r.repairs.append(f"{key} was {type(data.get(key)).__name__}; coerced to []")
            data[key] = default

    if not isinstance(data.get("first_action"), dict):
        r.repairs.append("first_action was not an object; coerced to empty fields")
        data["first_action"] = {"who": "", "contacts": "", "ask": ""}
    else:
        for f in ("who", "contacts", "ask"):
            data["first_action"].setdefault(f, "")

    if not isinstance(data.get("searches_used"), int):
        try:
            data["searches_used"] = int(data.get("searches_used", 0))
            r.repairs.append("searches_used coerced to int")
        except (TypeError, ValueError):
            data["searches_used"] = 0
            r.repairs.append("searches_used was unparseable; set to 0")

    path = data.get("path")
    if path is not None and not isinstance(path, list):
        r.errors.append(f"path must be a list or null, got {type(path).__name__}")
        path = None
        data["path"] = None

    # --- path nodes ----------------------------------------------------------
    if isinstance(path, list):
        for i, node in enumerate(path):
            if not isinstance(node, dict):
                r.errors.append(f"path[{i}] is not an object")
                continue
            for f in ("name", "role", "org"):
                if not _is_str(node.get(f)):
                    r.errors.append(f"path[{i}].{f} missing or not a string")
        if len(path) > MAX_PATH_NODES:
            r.errors.append(
                f"path has {len(path) - 2} intermediaries; "
                f"maximum is {MAX_INTERMEDIARIES} (PATH LENGTH rule)"
            )
        if len(path) < 2:
            r.errors.append("path must contain at least the starting person and the target")

    # --- hops ----------------------------------------------------------------
    hops = data["hops"]
    for i, hop in enumerate(hops):
        if not isinstance(hop, dict):
            r.errors.append(f"hops[{i}] is not an object")
            continue
        for f in ("from", "to", "basis", "source_url", "source_type", "evidence_date"):
            if not _is_str(hop.get(f)):
                r.errors.append(f"hops[{i}].{f} missing or not a string")
        if hop.get("strength") not in STRENGTHS:
            r.errors.append(
                f"hops[{i}].strength is {hop.get('strength')!r}, expected one of {STRENGTHS}"
            )
        url = hop.get("source_url")
        if _is_str(url) and not url.lower().startswith(("http://", "https://")):
            r.errors.append(f"hops[{i}].source_url is not an http(s) URL: {url!r}")

    # --- path/hop consistency ------------------------------------------------
    if isinstance(path, list) and path and hops:
        if len(hops) != len(path) - 1:
            r.errors.append(
                f"expected {len(path) - 1} hops for a {len(path)}-node path, got {len(hops)}"
            )
        else:
            for i, hop in enumerate(hops):
                if not isinstance(hop, dict) or not isinstance(path[i], dict):
                    continue
                if hop.get("from") != path[i].get("name"):
                    r.warnings.append(
                        f"hops[{i}].from ({hop.get('from')!r}) does not match "
                        f"path[{i}].name ({path[i].get('name')!r})"
                    )
                if hop.get("to") != path[i + 1].get("name"):
                    r.warnings.append(
                        f"hops[{i}].to ({hop.get('to')!r}) does not match "
                        f"path[{i + 1}].name ({path[i + 1].get('name')!r})"
                    )

    # --- rating rules (mechanically derivable → repair) ----------------------
    if path is None:
        if data.get("rating") != "dropped":
            r.repairs.append(f"rating was {data.get('rating')!r} with a null path; set to 'dropped'")
            data["rating"] = "dropped"
        if hops:
            r.repairs.append("hops were present with a null path; cleared")
            data["hops"] = hops = []
        if not data["weak_points"]:
            r.errors.append("path is null but weak_points is empty (no reason given)")
    else:
        if data.get("rating") == "dropped":
            r.errors.append("rating is 'dropped' but path is not null")
        elif not hops:
            r.errors.append("path is present but hops is empty")
        else:
            derived = weakest([h.get("strength", "weak") for h in hops if isinstance(h, dict)])
            if data.get("rating") != derived:
                r.repairs.append(
                    f"rating was {data.get('rating')!r}; set to {derived!r} (weakest hop)"
                )
                data["rating"] = derived

    if data.get("rating") not in RATINGS:
        r.errors.append(f"rating {data.get('rating')!r} is not one of {RATINGS}")

    r.warnings.extend(scan_for_pii(data))

    if strict and r.errors:
        raise SchemaError("; ".join(r.errors))
    return r


def scan_for_pii(data: dict[str, Any]) -> list[str]:
    """Flag output that appears to violate the SCOPE LIMIT section."""
    found: list[str] = []
    haystack = _walk_strings(data)
    for label, pattern in _PII_PATTERNS.items():
        for m in pattern.finditer(haystack):
            snippet = m.group(0)
            # Source URLs legitimately contain digit runs; don't flag those.
            if label == "phone" and "http" in haystack[max(0, m.start() - 60) : m.start()]:
                continue
            found.append(f"possible {label} in output: {snippet!r} — SCOPE LIMIT violation")
    return found


def _walk_strings(node: Any) -> str:
    out: list[str] = []

    def rec(n: Any) -> None:
        if isinstance(n, str):
            out.append(n)
        elif isinstance(n, dict):
            for v in n.values():
                rec(v)
        elif isinstance(n, list):
            for v in n:
                rec(v)

    rec(node)
    return "\n".join(out)


def lint_input(payload: dict[str, Any]) -> list[str]:
    """Warn about inputs that will produce the wrong person.

    'Charlie Warren' alone finds the wrong person; 'Charlie Warren, Visiting
    Partner at Y Combinator' does not. This does not block — some names are
    genuinely unambiguous — but a bare name is the single most common reason a
    path comes back about someone else entirely.
    """
    warnings: list[str] = []
    qualifier_markers = (",", " at ", " of ", "(", " — ", " - ")
    for field in ("target", "starting_person"):
        value = payload.get(field, "")
        if not isinstance(value, str) or not value.strip():
            warnings.append(f"{field} is empty")
            continue
        if not any(mark in value for mark in qualifier_markers):
            warnings.append(
                f"{field} is a bare name ({value!r}) with no role/org qualifier — "
                "high risk of resolving to the wrong person"
            )
    return warnings
