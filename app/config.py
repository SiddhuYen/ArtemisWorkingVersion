"""Configuration for the Artemis V6 wrapper.

Only what the surface actually needs: database URLs, auth, rate limits and the
build queue. The engine tuning that used to live here (search providers, silo
budgets, extraction thresholds, crawl limits) went with the engine.
"""
from __future__ import annotations

import os


def _env_bool(name: str, default: str) -> bool:
    """Parse a boolean env var, recognizing common truthy/falsy spellings
    (0/1, true/false, yes/no, on/off) case-insensitively. A value that isn't
    a recognized falsy spelling is truthy, so a typo like "flase" still
    behaves as an explicit override rather than silently doing nothing --
    but "no"/"off" (easy things to type meaning "disable this") are honored
    instead of being silently treated as enabled."""
    val = os.environ.get(name, default).strip().lower()
    return val not in ("0", "false", "no", "off", "n", "")


# --- storage ---------------------------------------------------------------
def _normalize_db_url(url: str) -> str:
    """Accept the `postgres://` scheme that hosted providers hand out.

    Supabase, Render and Heroku all still print connection strings starting
    `postgres://`, but SQLAlchemy 1.4+ removed that alias and raises
    NoSuchModuleError ("Can't load plugin: sqlalchemy.dialects:postgres") on
    it. Rewriting here means a copy-pasted dashboard string just works instead
    of failing at import time with an error that reads like a missing driver.
    """
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://"):]
    return url


def _resolve_db_url() -> str:
    """ARTEMIS_DB_URL wins; DATABASE_URL is the fallback; SQLite is the default.

    DATABASE_URL is the convention every managed host and Postgres add-on
    populates automatically (Render, Supabase, Heroku, Fly). Reading it means a
    deploy needs no Artemis-specific database wiring at all -- attach the
    database and the app finds it. ARTEMIS_DB_URL still takes precedence so a
    local override can point somewhere else without unsetting the platform's
    own variable.

    The SQLite default is for local dev and the test suite ONLY. It is not
    safe for a deployment: concurrent expansion has multiple writers, and
    SQLite serializes them behind a single write lock (see DEPLOY.md).
    """
    return _normalize_db_url(
        os.environ.get("ARTEMIS_DB_URL")
        or os.environ.get("DATABASE_URL")
        or "sqlite:///./artemis.db"
    )


DB_URL = _resolve_db_url()
IS_POSTGRES = DB_URL.startswith("postgresql")
# Per-session graph isolation (HTTP API): each browser session gets its own
# SQLite file here, so concurrent users never clobber each other's public graph.
GRAPH_DIR = os.environ.get("ARTEMIS_GRAPH_DIR", "./graphs")
# Boards/pages default to their OWN SQLite file: they never reference the
# discovery-graph tables, and isolating them means a board's frequent autosave
# writes never hit "database is locked" behind a long /connect or
# /targets/search transaction on the (much busier) main DB_URL database.
#
# That whole rationale is SQLite-specific, so on Postgres boards default to
# the SAME database instead -- there is no single write lock to contend for,
# and a deployment should not need a second managed database (or a writable
# local disk, which a container may not even have) just to hold two tables.
BOARDS_DB_URL = _normalize_db_url(
    os.environ.get("ARTEMIS_BOARDS_DB_URL")
    or (DB_URL if IS_POSTGRES else "sqlite:///./artemis_boards.db")
)
# Postgres connection pool (ignored on SQLite, which has no network pool).
# Sized against what expansion actually opens: one Session per concurrently
# processed node (EXPAND_NODE_CONCURRENCY, below), doubled because
# connect_people expands BOTH endpoints at once, plus the API's own
# per-request sessions. Default 10+10 covers that with headroom while staying
# far under the connection cap of a small managed tier -- which is shared with
# psql, migrations, and any other client you have open.
DB_POOL_SIZE = int(os.environ.get("ARTEMIS_DB_POOL_SIZE", "10"))
DB_MAX_OVERFLOW = int(os.environ.get("ARTEMIS_DB_MAX_OVERFLOW", "10"))


# --- auth -------------------------------------------------------------------
ACCESS_TOKEN = os.environ.get("ARTEMIS_ACCESS_TOKEN", "").strip()


def _parse_operators(raw: str) -> list[tuple[str, str, str]]:
    """`ARTEMIS_OPERATORS` -> [(operator_id, display_name, token), ...].

    Format is comma-separated entries, each either

        id:token                 display name defaults to the id
        id:Display Name:token

    Tokens are url-safe base64 (`secrets.token_urlsafe`) and never contain a
    colon, so the split is unambiguous.

    This exists because the app previously had no per-user identity at all:
    one shared token produced one identical session cookie for everybody, so
    nothing in a request could say WHO was asking, and owner scoping fell back
    to a caller-supplied string. One token per operator is the smallest change
    that makes identity server-resolved — no user table, no password store.
    """
    out: list[tuple[str, str, str]] = []
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = [p.strip() for p in entry.split(":")]
        if len(parts) == 2:
            oid, token = parts
            name = oid
        elif len(parts) >= 3:
            oid, name, token = parts[0], ":".join(parts[1:-1]), parts[-1]
        else:
            continue
        if oid and token:
            out.append((oid, name or oid, token))
    return out


OPERATORS: list[tuple[str, str, str]] = _parse_operators(
    os.environ.get("ARTEMIS_OPERATORS", ""))

# Backwards compatibility: a deploy that only sets ARTEMIS_ACCESS_TOKEN keeps
# working as a single operator. It is still not caller-expressible — the id is
# resolved server-side either way.
if not OPERATORS and ACCESS_TOKEN:
    OPERATORS = [("default", "default", ACCESS_TOKEN)]

# The identity used when the gate is off entirely (a local checkout with no
# token configured). A constant, so even in open mode a caller cannot choose
# whose rows they read.
ANONYMOUS_OPERATOR_ID = "default"
# Browser session cookie set by POST /login. Holds a value DERIVED from the
# token, never the token itself (see auth.session_value).
SESSION_COOKIE = os.environ.get("ARTEMIS_SESSION_COOKIE", "artemis_session")
SESSION_MAX_AGE_S = int(os.environ.get("ARTEMIS_SESSION_MAX_AGE_S", str(30 * 86400)))
TRUST_PROXY_HEADERS = _env_bool("ARTEMIS_TRUST_PROXY_HEADERS", "1")


# --- rate limits ------------------------------------------------------------
BUILD_RATE_LIMIT = int(os.environ.get("ARTEMIS_BUILD_RATE_LIMIT", "12"))
BUILD_RATE_WINDOW_S = float(os.environ.get("ARTEMIS_BUILD_RATE_WINDOW_S", "3600"))
LOGIN_RATE_LIMIT = int(os.environ.get("ARTEMIS_LOGIN_RATE_LIMIT", "10"))
LOGIN_RATE_WINDOW_S = float(os.environ.get("ARTEMIS_LOGIN_RATE_WINDOW_S", "900"))


# --- build queue ------------------------------------------------------------
MAX_CONCURRENT_BUILDS = int(os.environ.get("ARTEMIS_MAX_CONCURRENT_BUILDS", "2"))
MAX_QUEUED_BUILDS = int(os.environ.get("ARTEMIS_MAX_QUEUED_BUILDS", "8"))

# Kept because buildqueue reserves slots for it even though the enrichment
# feature itself is gone; a value of 0 would change queue behaviour.
ENRICH_RESERVED_BUILD_SLOTS = 0
