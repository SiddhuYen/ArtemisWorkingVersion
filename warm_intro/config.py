"""Configuration for the pathfinder."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal


@dataclass(frozen=True)
class Pricing:
    """Claude Opus 5 rate card, USD per million tokens.

    Cache writes bill at 1.25x input, cache reads at 0.1x. Web-search server-tool
    charges are NOT included here — `UsageRecord.web_searches` is reported
    separately so you can apply the current per-search rate yourself.
    """

    input_per_mtok: float = 5.00
    output_per_mtok: float = 25.00
    cache_write_multiplier: float = 1.25
    cache_read_multiplier: float = 0.10


@dataclass(frozen=True)
class PathfinderConfig:
    model: str = "claude-opus-5"
    max_tokens: int = 32_000
    # Opus 5 supports low|medium|high|xhigh|max. `high` is a sane default for
    # judgment work; sweep down to `medium` on your own eval set before assuming
    # you need more. Raise to `xhigh` only if you measure a lift.
    effort: str = "high"
    # None -> thinking blocks arrive with empty text (the Opus 5 default).
    # "summarized" -> readable reasoning summary, useful while tuning the prompt.
    thinking_display: Literal["summarized"] | None = None

    # --- search budget -------------------------------------------------------
    # `max_uses` on the tool is the real cost bound. It lives in `tools`, which
    # renders at position 0 of the prompt — so varying it per request invalidates
    # the entire cache, including the system prompt. Keep it pinned to a fixed
    # ceiling and let the per-request `max_searches` ride in the user message.
    search_ceiling: int = 12
    # Set True to enforce each request's own max_searches at the tool level.
    # Correct, but every distinct max_searches value gets its own cold cache.
    pin_max_uses_to_request: bool = False
    # Off by default (the spec enables web search only). Turning this on lets the
    # model read the pages it cites rather than judging them from a snippet,
    # which materially improves sourcing — at the cost of fetches that
    # `max_searches` does not count.
    enable_web_fetch: bool = False

    # --- reliability ---------------------------------------------------------
    # Server-tool turns can stop with `pause_turn`; each continuation is a
    # re-send of the conversation. 12 searches rarely needs more than a couple.
    max_pause_turns: int = 6
    max_parse_retries: int = 1
    # Rounds of client-side tool calls (e.g. looking up the operator's own
    # contacts) before the run gives up. Each round is one extra API request.
    max_tool_rounds: int = 8
    enable_fallbacks: bool = True

    # --- caching -------------------------------------------------------------
    # None -> 5 minute TTL. "1h" costs 2x on write but survives gaps between
    # batches; worth it only if you make 3+ calls per hour with idle time.
    cache_ttl: Literal["1h", "5m"] | None = None

    pricing: Pricing = field(default_factory=Pricing)

    # Called once per find_path() with the usage dict. Wire this to your metrics
    # sink; token counts vary a lot with how many searches the model runs, and
    # you want the real numbers rather than an estimate.
    usage_sink: Callable[[dict], None] | None = None

    # Called with short human-readable progress lines as the run proceeds. Every
    # line reports something that actually happened (a completed model turn, the
    # real search count) rather than a predicted stage, so a UI can show a live
    # transcript without inventing activity. Must not raise.
    progress_sink: Callable[[str], None] | None = None

    def __post_init__(self) -> None:
        if self.effort not in {"low", "medium", "high", "xhigh", "max"}:
            raise ValueError(f"invalid effort: {self.effort!r}")
        if self.search_ceiling < 1:
            raise ValueError("search_ceiling must be >= 1")
