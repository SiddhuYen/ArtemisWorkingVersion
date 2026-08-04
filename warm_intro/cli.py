"""CLI: JSON in, JSON out.

    echo '{"target": "...", "starting_person": "...", "ask": "...", "max_searches": 12}' \\
      | warm-intro

    warm-intro --target "Dana White, CEO of TKO Group Holdings" \\
               --from "Charlie Warren, Visiting Partner at Y Combinator" \\
               --ask "partnership conversation about a portfolio company"

Exit codes: 0 result (including a legitimate "dropped"), 1 wrapper failure,
2 bad invocation, 3 refusal.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from .client import PathfinderError, RefusalError, find_path, verify_existing
from .config import PathfinderConfig, VerifyConfig


def load_dotenv(path: Path | None = None) -> list[str]:
    """Populate os.environ from a local .env file. Returns the names it set.

    Deliberately dependency-free, and deliberately does not overwrite variables
    that are already set — a checked-out .env should never silently shadow a key
    you exported on purpose. Returns names only; values are never logged.
    """
    path = path or Path.cwd() / ".env"
    if not path.is_file():
        return []
    loaded: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="warm-intro",
        description="Find a sourced warm-introduction path between two people.",
    )
    src = p.add_argument_group("input (stdin JSON, or these flags)")
    src.add_argument("--target", help="the person to reach — include role and org")
    src.add_argument("--from", dest="starting_person", help="who the introduction originates from")
    src.add_argument("--ask", help="what the introduction is for")
    src.add_argument("--max-searches", type=int, default=12, help="hard cap (default: 12)")

    run = p.add_argument_group("run")
    run.add_argument("--model", default=PathfinderConfig.model)
    run.add_argument(
        "--effort",
        default=PathfinderConfig.effort,
        choices=["low", "medium", "high", "xhigh", "max"],
    )
    run.add_argument(
        "--search-ceiling",
        type=int,
        default=PathfinderConfig.search_ceiling,
        help="tool-level max_uses, held constant to preserve the prompt cache",
    )
    run.add_argument(
        "--pin-max-uses",
        action="store_true",
        help="enforce this request's max_searches at the tool level (costs a cache miss "
        "whenever the value changes)",
    )
    run.add_argument("--web-fetch", action="store_true", help="also enable the web_fetch tool")
    run.add_argument(
        "--show-thinking",
        action="store_true",
        help="request summarized reasoning (visible in --envelope logs at DEBUG)",
    )
    run.add_argument("--no-fallbacks", action="store_true", help="disable server-side refusal fallbacks")
    run.add_argument("--cache-ttl", choices=["5m", "1h"], default=None)

    out = p.add_argument_group("output")
    out.add_argument("--no-verify", action="store_true", help="skip source_url verification")
    out.add_argument(
        "--verify-only",
        action="store_true",
        help="no API call: read a result object on stdin and re-run the schema and "
        "source-verification layers over it (needs no credentials)",
    )
    out.add_argument(
        "--envelope",
        action="store_true",
        help="print result plus usage, validation report, and per-URL checks",
    )
    out.add_argument("--strict", action="store_true", help="exit non-zero on any schema violation")
    out.add_argument("--indent", type=int, default=2)
    out.add_argument("--log-level", default="WARNING")
    return p


def _read_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.target and args.starting_person and args.ask:
        return {
            "target": args.target,
            "starting_person": args.starting_person,
            "ask": args.ask,
            "max_searches": args.max_searches,
        }
    if sys.stdin.isatty():
        raise SystemExit(
            "provide --target, --from and --ask, or pipe the input JSON on stdin"
        )
    raw = sys.stdin.read().strip()
    if not raw:
        raise SystemExit("empty stdin and no --target/--from/--ask")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"stdin is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("stdin JSON must be an object")
    payload.setdefault("max_searches", args.max_searches)
    return payload


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.WARNING),
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    for name in load_dotenv():
        logging.getLogger("warm_intro").debug("loaded %s from .env", name)

    if args.verify_only:
        raw = sys.stdin.read().strip()
        if not raw:
            print("--verify-only expects a result JSON object on stdin", file=sys.stderr)
            return 2
        try:
            existing = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"stdin is not valid JSON: {exc}", file=sys.stderr)
            return 2
        cfg = PathfinderConfig(verify=VerifyConfig(enabled=not args.no_verify))
        try:
            result = verify_existing(existing, config=cfg, strict=args.strict)
        except PathfinderError as exc:
            print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
            return 1
        print(json.dumps(result.as_envelope() if args.envelope else result.data,
                         indent=args.indent, ensure_ascii=False))
        return 0

    try:
        payload = _read_payload(args)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2

    cfg = PathfinderConfig(
        model=args.model,
        effort=args.effort,
        search_ceiling=args.search_ceiling,
        pin_max_uses_to_request=args.pin_max_uses,
        enable_web_fetch=args.web_fetch,
        enable_fallbacks=not args.no_fallbacks,
        cache_ttl=args.cache_ttl,
        thinking_display="summarized" if args.show_thinking else None,
        verify=VerifyConfig(enabled=not args.no_verify),
    )

    try:
        result = find_path(payload, config=cfg, strict=args.strict)
    except RefusalError as exc:
        print(json.dumps({"error": "refusal", "category": exc.category,
                          "explanation": exc.explanation}), file=sys.stderr)
        return 3
    except (PathfinderError, ValueError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 1

    payload_out = result.as_envelope() if args.envelope else result.data
    print(json.dumps(payload_out, indent=args.indent, ensure_ascii=False))

    for warning in result.validation["warnings"] + result.input_warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
