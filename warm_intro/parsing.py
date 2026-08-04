"""Defensive extraction of one JSON object from model output.

We do not prefill `{`. Prefilling the assistant turn returns a 400 on Opus 5, and
the reason the original note gives (it conflicts with extended thinking) points
at the same place: on this model thinking is on by default. So the model emits
the object as ordinary text and we parse it out.
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"^\s*```(?:json|JSON)?\s*\n(.*?)\n?\s*```\s*$", re.DOTALL)


class JSONExtractionError(ValueError):
    """Raised when no single JSON object could be recovered from the text."""


def strip_fences(text: str) -> str:
    m = _FENCE.match(text.strip())
    return m.group(1) if m else text.strip()


def _find_object_span(text: str) -> tuple[int, int]:
    """Locate the first balanced top-level {...}, respecting strings and escapes."""
    start = text.find("{")
    if start == -1:
        raise JSONExtractionError("no '{' in model output")

    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return start, i + 1
    raise JSONExtractionError("unbalanced braces — output was likely truncated")


def extract_json_object(text: str) -> dict[str, Any]:
    """Return the single JSON object in `text`.

    Tolerates markdown fences and leading/trailing prose. Raises
    JSONExtractionError otherwise; the caller retries once.
    """
    candidate = strip_fences(text)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = _find_object_span(candidate)
        try:
            parsed = json.loads(candidate[start:end])
        except json.JSONDecodeError as exc:
            raise JSONExtractionError(f"invalid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise JSONExtractionError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


def collect_text(content: list[Any]) -> str:
    """Concatenate the text blocks of a response, ignoring thinking and tool blocks."""
    parts: list[str] = []
    for block in content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts)
