"""The pathfinder call: cached prefix, bounded search, verified sources."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from anthropic import Anthropic

from .config import PathfinderConfig
from .parsing import JSONExtractionError, collect_text, extract_json_object
from .prompt import REFORMAT_INSTRUCTION, SYSTEM_PROMPT
from .schema import ValidationReport, downgrade, drop_path, lint_input, validate_and_repair
from .verify import UrlCheck, verify_hops

log = logging.getLogger("warm_intro")

FALLBACK_BETA = "server-side-fallback-2026-07-01"

# `source_type` marking a hop attested by the starting person's own imported
# connections rather than by a public page. Kept in step with the value the
# system prompt asks for; hops carrying it skip URL verification.
OPERATOR_SOURCE = "operator_network"


def _progress(cfg: "PathfinderConfig", line: str) -> None:
    """Emit a progress line. A broken sink must never fail the run."""
    if not cfg.progress_sink:
        return
    try:
        cfg.progress_sink(line)
    except Exception:  # noqa: BLE001 - a UI transcript is not worth an exception
        log.debug("progress_sink raised", exc_info=True)


class PathfinderError(RuntimeError):
    """Base class for wrapper failures."""


class RefusalError(PathfinderError):
    """Safety classifiers declined the request, and the fallback chain did too."""

    def __init__(self, category: str | None, explanation: str | None) -> None:
        super().__init__(f"request refused (category={category}): {explanation}")
        self.category = category
        self.explanation = explanation


class TruncatedError(PathfinderError):
    """The response hit max_tokens. Raise max_tokens or lower effort."""


class UnparseableError(PathfinderError):
    """The model never produced a single JSON object, even after the retry."""


@dataclass
class ClientTool:
    """A tool Claude calls that we execute locally, rather than Anthropic's servers.

    Web search runs server-side and needs no loop. Anything backed by our own
    data — the operator's imported contacts — has to come back to us, so a turn
    that requests one stops with `tool_use` and is resumed with the result.
    """

    definition: dict[str, Any]           # {"name", "description", "input_schema"}
    handler: Any                         # Callable[[dict], str]

    @property
    def name(self) -> str:
        return str(self.definition.get("name", ""))


@dataclass
class UsageRecord:
    api_calls: int = 0
    tool_rounds: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    web_searches: int = 0
    search_errors: list[str] = field(default_factory=list)
    pause_turns: int = 0
    parse_retries: int = 0
    served_by: str | None = None
    fallback_used: bool = False
    stop_reason: str | None = None
    latency_s: float = 0.0

    def cost_usd(self, pricing) -> dict[str, float]:
        m = 1_000_000
        uncached = self.input_tokens / m * pricing.input_per_mtok
        writes = (
            self.cache_creation_input_tokens
            / m
            * pricing.input_per_mtok
            * pricing.cache_write_multiplier
        )
        reads = (
            self.cache_read_input_tokens
            / m
            * pricing.input_per_mtok
            * pricing.cache_read_multiplier
        )
        out = self.output_tokens / m * pricing.output_per_mtok
        return {
            "uncached_input": round(uncached, 6),
            "cache_write": round(writes, 6),
            "cache_read": round(reads, 6),
            "output": round(out, 6),
            # Web-search server-tool charges are not included; apply your current
            # per-search rate to `web_searches`.
            "total_tokens_only": round(uncached + writes + reads + out, 6),
        }

    def as_dict(self, pricing) -> dict[str, Any]:
        d = {
            "api_calls": self.api_calls,
            "tool_rounds": self.tool_rounds,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_hit": self.cache_read_input_tokens > 0,
            "web_searches": self.web_searches,
            "search_errors": self.search_errors,
            "pause_turns": self.pause_turns,
            "parse_retries": self.parse_retries,
            "served_by": self.served_by,
            "fallback_used": self.fallback_used,
            "stop_reason": self.stop_reason,
            "latency_s": round(self.latency_s, 3),
        }
        d["cost_usd"] = self.cost_usd(pricing)
        return d


@dataclass
class PathResult:
    """`data` is the schema-conformant object. Everything else is diagnostics."""

    data: dict[str, Any]
    usage: dict[str, Any]
    validation: dict[str, list[str]]
    url_checks: list[dict[str, Any]]
    input_warnings: list[str]
    raw_text: str

    def as_envelope(self) -> dict[str, Any]:
        return {
            "result": self.data,
            "usage": self.usage,
            "validation": self.validation,
            "url_checks": self.url_checks,
            "input_warnings": self.input_warnings,
        }


# --------------------------------------------------------------------------- #
# request construction
# --------------------------------------------------------------------------- #

def build_system(cfg: PathfinderConfig) -> list[dict[str, Any]]:
    """One text block carrying the whole prompt, with the cache breakpoint on it.

    Tools render before system, so this single breakpoint caches tools + system
    together — which is why `tools` must be byte-stable across calls too.
    """
    cache_control: dict[str, Any] = {"type": "ephemeral"}
    if cfg.cache_ttl:
        cache_control["ttl"] = cfg.cache_ttl
    return [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": cache_control}]


def build_tools(cfg: PathfinderConfig, max_searches: int) -> list[dict[str, Any]]:
    """`max_uses` is the hard cost bound.

    By default it is pinned to `search_ceiling` rather than this request's
    `max_searches`, because the tool list is the very first thing in the prompt:
    varying it per request gives every distinct budget its own cold cache. The
    per-request number still reaches the model in the user payload, and the true
    number of searches performed is counted from the response either way.
    """
    uses = max_searches if cfg.pin_max_uses_to_request else cfg.search_ceiling
    tools: list[dict[str, Any]] = [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": uses}
    ]
    if cfg.enable_web_fetch:
        tools.append({"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": uses})
    return tools


def build_user_message(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# response accounting
# --------------------------------------------------------------------------- #

def _accumulate(message: Any, usage: UsageRecord) -> None:
    usage.api_calls += 1
    u = getattr(message, "usage", None)
    if u is not None:
        usage.input_tokens += getattr(u, "input_tokens", 0) or 0
        usage.output_tokens += getattr(u, "output_tokens", 0) or 0
        usage.cache_creation_input_tokens += getattr(u, "cache_creation_input_tokens", 0) or 0
        usage.cache_read_input_tokens += getattr(u, "cache_read_input_tokens", 0) or 0
        for it in getattr(u, "iterations", None) or []:
            if getattr(it, "type", None) == "fallback_message":
                usage.fallback_used = True

    usage.served_by = getattr(message, "model", None) or usage.served_by
    usage.stop_reason = getattr(message, "stop_reason", None)

    # `usage.server_tool_use.web_search_requests` is the billed, authoritative
    # count. Counting server_tool_use blocks is only a fallback: it misses
    # searches when the tool is configured to omit nested result blocks from the
    # response, and that undercount would silently understate real spend.
    counted_from_usage = False
    stu = getattr(getattr(message, "usage", None), "server_tool_use", None)
    if stu is not None:
        n = getattr(stu, "web_search_requests", None)
        if n is None and isinstance(stu, dict):
            n = stu.get("web_search_requests")
        if isinstance(n, int):
            usage.web_searches += n
            counted_from_usage = True

    for block in getattr(message, "content", []) or []:
        btype = getattr(block, "type", None)
        if btype == "server_tool_use" and getattr(block, "name", None) in ("web_search", "web_fetch"):
            if not counted_from_usage:
                usage.web_searches += 1
        elif btype in ("web_search_tool_result", "web_fetch_tool_result"):
            content = getattr(block, "content", None)
            # A success payload is a list of results; an error payload is a
            # single object carrying error_code.
            if not isinstance(content, list):
                code = getattr(content, "error_code", None)
                if code is None and isinstance(content, dict):
                    code = content.get("error_code")
                if code:
                    usage.search_errors.append(str(code))


def _check_stop(message: Any) -> None:
    stop = getattr(message, "stop_reason", None)
    if stop == "refusal":
        details = getattr(message, "stop_details", None)
        raise RefusalError(
            getattr(details, "category", None),
            getattr(details, "explanation", None),
        )
    if stop == "max_tokens":
        raise TruncatedError(
            "response hit max_tokens — raise PathfinderConfig.max_tokens or lower effort"
        )


# --------------------------------------------------------------------------- #
# the call
# --------------------------------------------------------------------------- #

def _run_turn(
    client: Anthropic,
    cfg: PathfinderConfig,
    messages: list[dict[str, Any]],
    system: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    usage: UsageRecord,
    client_tools: list["ClientTool"] | None = None,
) -> Any:
    """Send one logical turn, resuming through any `pause_turn` interruptions.

    Server-tool turns stop with `pause_turn` when the server-side loop hits its
    iteration limit. Resuming means re-sending the conversation with the paused
    assistant turn appended — no extra user message.
    """
    kwargs: dict[str, Any] = {
        "model": cfg.model,
        "max_tokens": cfg.max_tokens,
        "system": system,
        "tools": tools,
        "output_config": {"effort": cfg.effort},
    }
    if cfg.thinking_display:
        kwargs["thinking"] = {"type": "adaptive", "display": cfg.thinking_display}
    if cfg.enable_fallbacks:
        kwargs["betas"] = [FALLBACK_BETA]
        kwargs["fallbacks"] = "default"

    by_name = {t.name: t for t in (client_tools or [])}
    pauses = 0
    rounds = 0

    while True:
        # Streaming, because a 20-search research turn runs long enough to hit
        # the SDK's non-streaming HTTP timeout.
        with client.beta.messages.stream(messages=messages, **kwargs) as stream:
            message = stream.get_final_message()

        _accumulate(message, usage)
        _check_stop(message)
        stop = getattr(message, "stop_reason", None)

        if stop == "pause_turn":
            usage.pause_turns += 1
            pauses += 1
            _progress(
                cfg,
                f"search paused at the server-side loop limit after "
                f"{usage.web_searches} searches — resuming",
            )
            if pauses > cfg.max_pause_turns:
                raise PathfinderError(
                    f"still paused after {cfg.max_pause_turns} continuations; "
                    "raise max_pause_turns or lower max_searches"
                )
            messages.append({"role": "assistant", "content": message.content})
            continue

        if stop == "tool_use":
            rounds += 1
            usage.tool_rounds += 1
            if rounds > cfg.max_tool_rounds:
                raise PathfinderError(
                    f"client tools still being called after {cfg.max_tool_rounds} "
                    "rounds; raise max_tool_rounds"
                )
            results: list[dict[str, Any]] = []
            for block in getattr(message, "content", []) or []:
                if getattr(block, "type", None) != "tool_use":
                    continue
                tool = by_name.get(getattr(block, "name", ""))
                if tool is None:
                    # A tool we did not register. Answer with an error rather
                    # than dropping it: every tool_use id must be resolved or
                    # the next request is rejected outright.
                    results.append({
                        "type": "tool_result", "tool_use_id": block.id,
                        "content": f"unknown tool: {getattr(block, 'name', '')}",
                        "is_error": True,
                    })
                    continue
                try:
                    output = tool.handler(dict(getattr(block, "input", {}) or {}))
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": str(output)})
                except Exception as exc:  # noqa: BLE001 — report, don't abort the run
                    log.warning("client tool %s failed", tool.name, exc_info=True)
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": f"tool error: {exc}", "is_error": True})
            if not results:
                raise PathfinderError(
                    "stop_reason was tool_use but the response carried no tool_use block"
                )
            messages.append({"role": "assistant", "content": message.content})
            messages.append({"role": "user", "content": results})
            continue

        return message


def find_path(
    payload: dict[str, Any],
    *,
    config: PathfinderConfig | None = None,
    client: Anthropic | None = None,
    strict: bool = False,
    client_tools: list[ClientTool] | None = None,
) -> PathResult:
    """Find a warm-introduction path.

    `payload` is the input object: target, starting_person, ask, max_searches.
    Returns a PathResult whose `.data` satisfies the output contract.
    """
    cfg = config or PathfinderConfig()
    # A bare Anthropic() resolves ANTHROPIC_API_KEY, then ANTHROPIC_AUTH_TOKEN,
    # then an `ant auth login` profile — do not require a key to be exported.
    client = client or Anthropic()

    payload = dict(payload)
    payload.setdefault("max_searches", cfg.search_ceiling)
    max_searches = int(payload["max_searches"])
    if not cfg.pin_max_uses_to_request and max_searches > cfg.search_ceiling:
        raise ValueError(
            f"max_searches={max_searches} exceeds search_ceiling={cfg.search_ceiling}. "
            "Raise search_ceiling (and accept one cold cache write), or set "
            "pin_max_uses_to_request=True to enforce per-request budgets at the "
            "cost of a cache miss whenever the budget changes."
        )

    input_warnings = lint_input(payload)
    for w in input_warnings:
        log.warning("input lint: %s", w)

    system = build_system(cfg)
    tools = build_tools(cfg, max_searches)
    # Client tools land after the server tools, in name order, so the tool block
    # stays byte-stable for a given set. A run WITH the network tool is a
    # different cached prefix from one without — two cache lines, not churn.
    for tool in sorted(client_tools or [], key=lambda t: t.name):
        tools.append(tool.definition)
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": build_user_message(payload)}
    ]

    usage = UsageRecord()
    started = time.monotonic()

    data: dict[str, Any] | None = None
    raw_text = ""
    last_error: Exception | None = None

    _progress(
        cfg,
        f"researching a route to {payload.get('target')} "
        f"from {payload.get('starting_person')} — up to {max_searches} searches",
    )

    for attempt in range(cfg.max_parse_retries + 1):
        message = _run_turn(client, cfg, messages, system, tools, usage, client_tools)
        raw_text = collect_text(message.content)
        _progress(cfg, f"research complete — {usage.web_searches} searches performed")
        try:
            data = extract_json_object(raw_text)
            break
        except JSONExtractionError as exc:
            last_error = exc
            if attempt == cfg.max_parse_retries:
                break
            usage.parse_retries += 1
            _progress(cfg, "output was not a single JSON object — asking again")
            log.warning("parse failure (attempt %d): %s", attempt + 1, exc)
            messages.append({"role": "assistant", "content": message.content})
            messages.append({"role": "user", "content": REFORMAT_INSTRUCTION})

    usage.latency_s = time.monotonic() - started

    if data is None:
        _emit_usage(cfg, usage, payload, ok=False)
        raise UnparseableError(
            f"no JSON object after {usage.parse_retries + 1} attempts: {last_error}"
        )

    # 1. Shape and rating rules, before anything reads the object.
    report = validate_and_repair(data, strict=strict)

    # 2. Source verification — the check that actually protects the caller.
    url_checks: list[UrlCheck] = []
    if cfg.verify.enabled and isinstance(data.get("path"), list) and data.get("hops"):
        hops = data["hops"]
        # A hop attested by the starting person's own network has no public URL
        # by design — the prompt tells the model to leave source_url empty for
        # those. Running the URL check over it fetched "", failed, and
        # downgraded the hop for following instructions, which dragged the whole
        # route's rating down to the weakest thing in it. There is nothing to
        # verify here: the evidence is the operator's own export, and we are the
        # ones who handed it to the model.
        verifiable = [(i, h) for i, h in enumerate(hops)
                      if isinstance(h, dict) and h.get("source_type") != OPERATOR_SOURCE]
        attested = len(hops) - len(verifiable)
        if verifiable:
            note = f"verifying {len(verifiable)} cited source(s)"
            if attested:
                note += f" ({attested} from your own network, no source to check)"
            _progress(cfg, note)
            url_checks = verify_hops(
                [h for _, h in verifiable],
                cfg.verify,
                on_progress=(lambda line: _progress(cfg, line)) if cfg.progress_sink else None,
            )
            _apply_verification(
                data, list(zip((i for i, _ in verifiable), url_checks)), report)
        elif attested:
            _progress(cfg, f"no public sources to check — all {attested} hop(s) "
                           "come from your own network")
        # Strengths and possibly the path itself changed; re-derive the rating.
        report2 = validate_and_repair(data, strict=False)
        report.errors.extend(report2.errors)
        report.repairs.extend(report2.repairs)
        report.warnings.extend(report2.warnings)

    # 3. searches_used is model-reported; replace it with what actually happened.
    claimed = data.get("searches_used")
    if claimed != usage.web_searches:
        report.repairs.append(
            f"searches_used was {claimed!r} (model-reported); "
            f"set to {usage.web_searches} (counted from server_tool_use blocks)"
        )
        data["searches_used"] = usage.web_searches
    if usage.web_searches > max_searches:
        report.warnings.append(
            f"budget overrun: {usage.web_searches} searches performed against "
            f"max_searches={max_searches} "
            f"(tool max_uses was {max_searches if cfg.pin_max_uses_to_request else cfg.search_ceiling})"
        )

    if strict and report.errors:
        _emit_usage(cfg, usage, payload, ok=False)
        raise PathfinderError("; ".join(report.errors))

    rating = data.get("rating")
    _progress(
        cfg,
        f"result: {rating}"
        + ("" if data.get("path") else " — no sourced route survived verification"),
    )
    _emit_usage(cfg, usage, payload, ok=True, rating=rating)

    return PathResult(
        data=data,
        usage=usage.as_dict(cfg.pricing),
        validation=report.as_dict(),
        url_checks=[c.as_dict() for c in url_checks],
        input_warnings=input_warnings,
        raw_text=raw_text,
    )


def verify_existing(
    data: dict[str, Any],
    *,
    config: PathfinderConfig | None = None,
    strict: bool = False,
) -> PathResult:
    """Run the schema and source-verification layers over an existing result.

    Makes no API call. Two uses: re-checking a stored path before someone acts on
    it — links rot and roles end, so a path verified last quarter is a claim
    again today — and exercising the verifier without credentials.
    """
    cfg = config or PathfinderConfig()
    data = json.loads(json.dumps(data))  # don't mutate the caller's object

    report = validate_and_repair(data, strict=strict)
    url_checks: list[UrlCheck] = []
    if cfg.verify.enabled and isinstance(data.get("path"), list) and data.get("hops"):
        url_checks = verify_hops(data["hops"], cfg.verify)
        _apply_verification(data, url_checks, report)
        second = validate_and_repair(data, strict=False)
        report.errors.extend(second.errors)
        report.repairs.extend(second.repairs)
        report.warnings.extend(second.warnings)

    if strict and report.errors:
        raise PathfinderError("; ".join(report.errors))

    return PathResult(
        data=data,
        usage=UsageRecord().as_dict(cfg.pricing),
        validation=report.as_dict(),
        url_checks=[c.as_dict() for c in url_checks],
        input_warnings=[],
        raw_text="",
    )


def _apply_verification(
    data: dict[str, Any],
    checks: list[tuple[int, UrlCheck]],
    report: ValidationReport,
) -> None:
    """Fold URL-check verdicts into hop strengths, dropping the path if needed.

    Takes (hop_index, check) pairs rather than a parallel list: hops that need no
    verification are skipped upstream, so positions no longer line up.
    """
    hops = data.get("hops") or []
    for index, check in checks:
        if index >= len(hops):
            continue
        hop = hops[index]
        if not isinstance(hop, dict):
            continue
        detail = check.note or check.error or check.verdict

        if check.action == "drop":
            reason = (
                f"source verification failed for hop "
                f"{hop.get('from')!r} -> {hop.get('to')!r}: {detail} ({check.url})"
            )
            report.warnings.append(reason)
            drop_path(data, reason)
            return

        if check.action == "downgrade":
            before = hop.get("strength", "weak")
            hop["strength"] = downgrade(before, "weak")
            note = (
                f"hop {hop.get('from')!r} -> {hop.get('to')!r} downgraded "
                f"{before} -> {hop['strength']}: {detail} ({check.url})"
            )
            report.repairs.append(note)
            wp = data.setdefault("weak_points", [])
            if isinstance(wp, list) and note not in wp:
                wp.append(note)


def _emit_usage(
    cfg: PathfinderConfig,
    usage: UsageRecord,
    payload: dict[str, Any],
    *,
    ok: bool,
    rating: str | None = None,
) -> None:
    record = usage.as_dict(cfg.pricing)
    record["ok"] = ok
    record["rating"] = rating
    record["target"] = payload.get("target")
    record["model_requested"] = cfg.model
    record["effort"] = cfg.effort
    log.info("warm_intro.usage %s", json.dumps(record, sort_keys=True))
    if cfg.usage_sink:
        cfg.usage_sink(record)
