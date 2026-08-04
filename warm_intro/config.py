"""Configuration for the pathfinder and the source verifier."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal, Mapping

Verdict = Literal[
    "verified",  # page fetched, both names present
    "names_missing",  # page fetched with real text, but a name is absent
    "soft_source",  # aggregator / contact-database domain
    "unverifiable",  # fetched but unreadable (PDF, JS shell, too little text)
    "unreachable",  # DNS/TLS/timeout/4xx/5xx
]

Action = Literal["keep", "downgrade", "drop"]


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
class VerifyConfig:
    """Server-side `source_url` verification.

    This is the highest-value check in the wrapper: it is the only thing standing
    between a plausible-sounding hop and a real person's inbox.
    """

    enabled: bool = True
    timeout_s: float = 15.0
    max_bytes: int = 2_000_000
    max_concurrency: int = 6
    # Identify honestly. This is not politeness theatre: sec.gov and the
    # Wikimedia sites both serve 403 to a spoofed browser User-Agent and 200 to
    # a descriptive one, and those are exactly the primary sources the prompt
    # tells the model to prefer. A fake Chrome UA turns verifiable SEC filings
    # into `unreachable` downgrades. Put a real contact address here.
    user_agent: str = "warm-intro/0.1 (source verification; contact: set-me@example.com)"
    # Below this many characters of extracted text we cannot honestly say a name
    # is absent — the page is a JS shell, a paywall stub, or a redirect notice.
    # Calling that `names_missing` would drop good paths on a rendering quirk.
    min_text_chars: int = 400
    # Domains the system prompt calls insufficient on their own. Substring match
    # against the final host.
    soft_domains: tuple[str, ...] = (
        "linkedin.com",
        "rocketreach.co",
        "zoominfo.com",
        "apollo.io",
        "signalhire.com",
        "lusha.com",
        "contactout.com",
        "peoplefinder",
        "spokeo.com",
    )
    # What each verdict does to the hop it backs.
    #
    # Nothing drops by default. Verification annotates a route; it does not veto
    # one. An earlier default dropped the whole path on `names_missing`, which
    # was too blunt in practice: a citation can be a real, correct source for a
    # relationship and still fail a literal both-names-on-the-page test — the
    # page renders a name in an image, the relationship is described a paragraph
    # later under a title rather than a name, the filing is an exhibit that
    # incorporates the roster by reference. Throwing the route away in those
    # cases loses a good answer and reports "no route found", which is the one
    # outcome an operator cannot act on or check.
    #
    # So a failed check now weakens the hop and says why, and the route is still
    # returned with its evidence attached for a human to judge. Set
    # `names_missing` back to "drop" if you would rather have silence than a
    # route whose source you have to read yourself.
    actions: Mapping[Verdict, Action] = field(
        default_factory=lambda: {
            "verified": "keep",
            "names_missing": "downgrade",
            "soft_source": "downgrade",
            "unverifiable": "downgrade",
            "unreachable": "downgrade",
        }
    )


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
    verify: VerifyConfig = field(default_factory=VerifyConfig)

    # Called once per find_path() with the usage dict. Wire this to your metrics
    # sink; token counts vary a lot with how many searches the model runs, and
    # you want the real numbers rather than an estimate.
    usage_sink: Callable[[dict], None] | None = None

    # Called with short human-readable progress lines as the run proceeds. Every
    # line reports something that actually happened (a completed model turn, a
    # source fetched and its verdict) rather than a predicted stage, so a UI can
    # show a live transcript without inventing activity. Must not raise.
    progress_sink: Callable[[str], None] | None = None

    def __post_init__(self) -> None:
        if self.effort not in {"low", "medium", "high", "xhigh", "max"}:
            raise ValueError(f"invalid effort: {self.effort!r}")
        if self.search_ceiling < 1:
            raise ValueError("search_ceiling must be >= 1")
