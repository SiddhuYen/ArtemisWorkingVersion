"""FastAPI surface for Artemis V6 — a pure wrapper around Claude with web search.

There is exactly one piece of intelligence in this application: `route_engine`,
which asks Claude (with its own web search tool) to find a warm-introduction
route and then independently re-fetches every source it cites. No search-API
providers, no spaCy, no local relationship graph, no extraction pipeline — the
22k-line engine that used to live here has been deleted, not disabled.

Everything else is scaffolding for that one call:

  POST /connect          find a route between two people   -> {"job_id"}
  POST /discover         map one person's connections      -> {"job_id"}
  GET  /jobs/{id}        status, percent, live log, result
  POST /jobs/{id}/cancel stop a running or queued job
  GET/POST/PATCH/DELETE /boards[/pages]   board persistence, so a found route
                                          has somewhere to be displayed
  GET  /status           credential + build-queue state
  GET  /health /healthz  liveness
  POST /login /logout    exchange the shared token for a session cookie

The board UI calls several endpoints belonging to features this build does not
implement (contact import, cliques, matching, candidate paths). They are stubbed
at the bottom of this file rather than removed, so the UI boots and behaves
instead of erroring on a 404 — see that section for what each one now returns.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from collections import defaultdict

from fastapi import (
    Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import auth, config, contacts
from .buildqueue import BUILDS, QueueFull
from .db import get_boards_db, init_boards_db, safe_graph_id
from .models import Board, BoardPage

app = FastAPI(
    title="Artemis V6 — warm-introduction routing",
    version="0.1.0",
    description="Finds sourced warm-introduction routes with Claude + web search, "
                "then verifies every cited source before returning a path.",
)


@app.on_event("startup")
def _startup() -> None:
    init_boards_db()


# ---------------------------------------------------------------------------
# Access control. One shared token gates the whole surface — UI and API — when
# config.ACCESS_TOKEN is set; unset, the app is open (a local checkout).
#
# Middleware rather than a per-route dependency so it also covers the mounted
# static UI and anything added later: a new endpoint is protected by default
# instead of protected only if someone remembered the dependency.
# ---------------------------------------------------------------------------
# Reachable without the token. /healthz so a load balancer can probe a locked
# app; the login route so there is a way to GET the form and POST the token.
_PUBLIC_PATHS = {"/healthz", "/login", "/logout"}


def _wants_html(request: Request) -> bool:
    return "text/html" in (request.headers.get("accept") or "")


@app.middleware("http")
async def _require_token(request: Request, call_next):
    if not auth.enabled() or request.url.path in _PUBLIC_PATHS:
        return await call_next(request)
    if auth.request_authenticated(request.headers, request.cookies):
        return await call_next(request)
    # A browser navigating to a page gets sent to the login form; an API client
    # gets a machine-readable 401 it can act on rather than an HTML redirect it
    # would have to sniff.
    if _wants_html(request):
        return RedirectResponse(url="/login", status_code=303)
    return JSONResponse({"detail": "authentication required"}, status_code=401)


def _client_key(request: Request) -> str:
    return auth.client_key(request.client.host if request.client else "", request.headers)


def _enforce_rate_limit(request: Request) -> None:
    """Charge one build token, or 429. Applied to the endpoints that spend
    money, never to reads — a throttled dashboard is just a broken dashboard."""
    allowed, retry_after = auth.build_limiter.check(_client_key(request))
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=(f"rate limit reached ({config.BUILD_RATE_LIMIT} builds per "
                    f"{int(config.BUILD_RATE_WINDOW_S // 60)} min) — "
                    f"retry in {int(retry_after)}s"),
            headers={"Retry-After": str(int(retry_after))},
        )


_LOGIN_PAGE = """<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Artemis — sign in</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 15px/1.5 ui-sans-serif, system-ui, sans-serif; display: grid;
         place-items: center; min-height: 100vh; margin: 0; }
  form { display: grid; gap: .75rem; width: min(22rem, 90vw); }
  h1 { font-size: 1.1rem; margin: 0 0 .25rem; }
  input, button { font: inherit; padding: .6rem .7rem; border-radius: .4rem;
                  border: 1px solid color-mix(in srgb, currentColor 30%, transparent); }
  button { cursor: pointer; font-weight: 600; }
  .err { color: #c0392b; min-height: 1.2em; font-size: .9em; }
</style>
<form id="f">
  <h1>Artemis</h1>
  <label for="t">Access token</label>
  <input id="t" type="password" autocomplete="current-password" autofocus>
  <button type="submit">Sign in</button>
  <div class="err" id="e"></div>
</form>
<script>
document.getElementById('f').addEventListener('submit', async ev => {
  ev.preventDefault();
  const e = document.getElementById('e');
  e.textContent = '';
  const res = await fetch('/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token: document.getElementById('t').value }),
  });
  if (res.ok) { location.href = '/ui/'; return; }
  let msg = 'Sign in failed.';
  try { msg = (await res.json()).detail || msg; } catch (_) {}
  e.textContent = msg;
});
</script>
"""


@app.get("/login", include_in_schema=False)
def login_page(request: Request):
    if auth.request_authenticated(request.headers, request.cookies):
        return RedirectResponse(url="/ui/", status_code=303)
    return HTMLResponse(_LOGIN_PAGE)


@app.post("/login")
def login(req: dict, request: Request) -> JSONResponse:
    """Exchange the shared token for a session cookie.

    Rate-limited per client independently of the build limiter: a public URL
    invites brute force, and a shared token has no lockout of its own.
    """
    if not auth.enabled():
        return JSONResponse({"ok": True, "auth_required": False})
    allowed, retry_after = auth.login_limiter.check(_client_key(request))
    if not allowed:
        raise HTTPException(status_code=429,
                            detail=f"too many attempts — retry in {int(retry_after)}s",
                            headers={"Retry-After": str(int(retry_after))})
    if not auth.token_ok(_str_field(req, "token")):
        raise HTTPException(status_code=401, detail="invalid token")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        config.SESSION_COOKIE, auth.session_value(),
        max_age=config.SESSION_MAX_AGE_S,
        httponly=True,          # unreadable from JS, so an XSS can't lift it
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return resp


@app.post("/logout")
def logout() -> JSONResponse:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(config.SESSION_COOKIE)
    return resp


# ---------------------------------------------------------------------------
# Background jobs — /discover and /connect can run minutes (live web search
# across multiple hops), far past what a single HTTP request should block on.
# Each POST spawns a worker thread and returns a job_id immediately; the UI
# polls GET /jobs/{id} for a real percent-complete (from expand_graph's
# on_step hop/node counters) instead of guessing at an indeterminate spinner.
# In-memory only — fine for a single-process deployment; a job that outlives
# the process (crash/restart) is simply gone, same as any other in-flight work.
# ---------------------------------------------------------------------------
_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()
_JOB_TTL_S = 3600  # stale finished jobs are swept opportunistically, not held forever


class JobCancelled(Exception):
    """Raised inside worker threads when a user cancels a background job."""


def _gc_jobs() -> None:
    cutoff = time.time() - _JOB_TTL_S
    stale = [jid for jid, j in _JOBS.items()
             if j["status"] not in {"running", "cancelling"} and j["updated_at"] < cutoff]
    for jid in stale:
        del _JOBS[jid]


def _new_job(kind: str = "job", ticket=None) -> str:
    job_id = uuid.uuid4().hex
    with _JOBS_LOCK:
        _gc_jobs()
        _JOBS[job_id] = {
            "kind": kind, "status": "queued", "pct": 0, "message": "queued…",
            "result": None, "error": None, "updated_at": time.time(),
            "cancel_requested": False, "_cancel_event": threading.Event(),
            # The build-queue ticket this job is waiting on, so /jobs/{id} can
            # report a real place in line instead of an opaque "queued…".
            "_ticket": ticket,
            # Live "thinking" transcript -- every graph.*'s free-text
            # progress() line (strategy decisions, org profiling verdicts,
            # homonym rejections, skip reasons), appended as they happen, not
            # just the coarse hop/node counters `message` carries. See
            # _append_job_log.
            "log": [],
        }
    return job_id


# A long build could otherwise accumulate an unbounded log -- bound it to the
# most recent lines; anything older has already scrolled past in a live-
# following UI and isn't needed for the final result either.
_JOB_LOG_MAX = 500


def _append_job_log(job_id: str, line: str) -> None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        log = job.setdefault("log", [])
        log.append(line)
        if len(log) > _JOB_LOG_MAX:
            del log[: len(log) - _JOB_LOG_MAX]
        job["updated_at"] = time.time()


def _update_job(job_id: str, **fields) -> None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        if job.get("status") in {"cancelling", "cancelled"} and "status" not in fields:
            return
        if job.get("_cancel_event") and job["_cancel_event"].is_set() and fields.get("status") in {"done", "error"}:
            fields = {"status": "cancelled", "pct": job.get("pct", 0),
                      "message": "cancelled", "error": None, "result": None}
        if "pct" in fields:
            pct = max(0, min(100, int(fields["pct"] or 0)))
            if fields.get("status") != "done":
                pct = max(int(job.get("pct", 0) or 0), pct)
            fields["pct"] = pct
        job.update(fields, updated_at=time.time())


def _public_job(job: dict) -> dict:
    """The client-visible shape: drop internals, add live queue position."""
    out = {k: v for k, v in job.items()
           if k != "updated_at" and not k.startswith("_")}
    ticket = job.get("_ticket")
    # Computed on read, not stored: position changes as other builds finish,
    # and a value written at enqueue time would be stale by the first poll.
    out["queue_position"] = BUILDS.position(ticket) if ticket is not None else 0
    return out


def _get_job(job_id: str) -> dict:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown or expired job")
        return _public_job(job)


def _job_cancel_event(job_id: str) -> threading.Event:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown or expired job")
        return job["_cancel_event"]


def _check_job_cancelled(job_id: str) -> None:
    if _job_cancel_event(job_id).is_set():
        raise JobCancelled()


def _await_build_slot(job_id: str, ticket, check_cancel) -> None:
    """Block until this job's ticket reaches the head of the build queue.

    Reports its place in line while it waits, so a queued user sees "waiting —
    2 build(s) ahead" rather than a progress bar stuck at 0 that is
    indistinguishable from a hang.
    """
    def _tick() -> None:
        # Cancel first: a user who gave up while queued should stop here, not
        # after being handed a slot. Raising leaves the ticket for the caller's
        # finally-block to release.
        check_cancel()
        pos = BUILDS.position(ticket)
        if pos > 0:
            # Message only, no status — _update_job's guard drops a
            # status-less update once a job is cancelling, so this can never
            # resurrect a job the user just cancelled.
            _update_job(job_id, message=f"waiting — {pos} build(s) ahead")

    _tick()
    BUILDS.acquire(ticket, check_cancel=_tick)
    _update_job(job_id, status="running", message="starting…")


def _start_build_job(request: Request, kind: str, worker, args: tuple) -> dict:
    """Admit a build, or refuse it. Every money-spending endpoint goes through here.

    Two gates, in order, both answered on the request thread so the client gets
    an immediate verdict rather than a job id that will never run:
      1. per-client rate limit  -> 429, this caller has had enough for now
      2. queue capacity         -> 429, the server is saturated for everyone
    """
    _enforce_rate_limit(request)
    try:
        ticket = BUILDS.reserve()
    except QueueFull:
        stats = BUILDS.stats()
        raise HTTPException(
            status_code=429,
            detail=(f"server busy — {stats['running']} build(s) running, "
                    f"{stats['queued']} queued. Try again in a minute."),
            headers={"Retry-After": "60"},
        )
    job_id = _new_job(kind, ticket=ticket)
    threading.Thread(target=worker, daemon=True,
                     args=(job_id, ticket) + args).start()
    return {"job_id": job_id}


def _str_field(req: dict, key: str) -> str:
    """Extract+strip a string field from a raw JSON request body.

    Endpoints below take `req: dict` (not a Pydantic model) so a client can
    send any JSON shape; a non-string value (e.g. {"person_a": 123}) must
    raise a clean 400 here rather than blow up on .strip() with a raw 500.
    """
    val = req.get(key)
    if val is None:
        return ""
    if not isinstance(val, str):
        raise HTTPException(status_code=400, detail=f"'{key}' must be a string")
    return val.strip()


def _owner_id(x_graph_id: str = Header(default="default", alias="X-Graph-Id")) -> str:
    """The per-browser id the frontend mints into localStorage. Scopes both the
    operator's own profile (/owner) and Boards; the discovery graph itself is
    shared. Defined up here because endpoints in several sections below take it
    as a dependency, and Depends() resolves at decoration time."""
    return safe_graph_id(x_graph_id)


@app.get("/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    return _get_job(job_id)


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown or expired job")
        if job["status"] in {"done", "error", "cancelled"}:
            return {k: v for k, v in job.items()
                    if k != "updated_at" and not k.startswith("_")}
        job["_cancel_event"].set()
        job.update(status="cancelling", cancel_requested=True,
                   message="cancelling…", updated_at=time.time())
    return _get_job(job_id)


# There is no shared graph. Every route is researched live and verified per run;
# nothing accumulates server-side except the boards a route is displayed on.

@app.get("/", include_in_schema=False)
def _root() -> RedirectResponse:
    return RedirectResponse(url="/ui/")


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    # cheap liveness probe — no dependency I/O at all, so it can't hang or flap.
    return {"status": "ok"}


def _anthropic_state() -> str:
    """Whether a credential is resolvable, without spending a request to prove it.

    A bare Anthropic() also accepts ANTHROPIC_AUTH_TOKEN or an `ant auth login`
    profile, so an unset ANTHROPIC_API_KEY does not mean "no credentials" —
    hence the client construction rather than an env-var check.
    """
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "configured"
    try:
        from anthropic import Anthropic
        return "configured" if Anthropic().api_key else "not_configured"
    except Exception:
        return "not_configured"


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "engine": "warm_intro", "anthropic": _anthropic_state()}


@app.get("/status")
def status() -> dict:
    """What the UI needs to know before it lets someone spend money.

    There is one credential and one search tool. Claude's own `web_search` is
    the only way this app touches the web, so an Anthropic credential is the
    only thing that decides whether a route can be run.
    """
    anthropic_state = _anthropic_state()
    return {
        "engine": "warm_intro",
        "anthropic": {"state": anthropic_state},
        "search": {"state": anthropic_state, "provider": "claude_web_search"},
        "auth": {"enabled": auth.enabled()},
        "builds": BUILDS.stats(),
    }


# ---------------------------------------------------------------------------
# The route finder. This is the whole product.
# ---------------------------------------------------------------------------
def _wrapper_job(job_id: str, ticket, fn, *args, **kwargs) -> None:
    """Run one wrapper call as a background job.

    Progress is a real transcript, not a simulated one: each line reports a
    completed model turn or a source that was actually fetched and judged. There
    is no per-hop counter to drive a percentage from, so `pct` advances on
    milestones and holds below 100 until the result is in.
    """
    def check_cancel() -> None:
        _check_job_cancelled(job_id)

    steps = {"n": 0}

    def progress(line: str) -> None:
        check_cancel()
        _append_job_log(job_id, line)
        steps["n"] += 1
        # Asymptotic: every milestone closes part of the remaining gap, so the
        # bar always moves and never reaches 100 before the job is done.
        _update_job(job_id, pct=int(95 - 95 * (0.72 ** steps["n"])), message=line[:120])

    try:
        _await_build_slot(job_id, ticket, check_cancel)
        check_cancel()
        result = fn(*args, on_progress=progress, **kwargs)
        check_cancel()
        # Not read by the UI; kept so an operator inspecting a job can see what
        # the call actually cost.
        usage = (result.get("warm_intro") or {}).get("usage")
        if usage:
            with _JOBS_LOCK:
                if job_id in _JOBS:
                    _JOBS[job_id]["claude_usage"] = usage
        _update_job(job_id, status="done", pct=100, message="done", result=result)
    except JobCancelled:
        _update_job(job_id, status="cancelled", message="cancelled", error=None, result=None)
    except Exception as exc:
        _update_job(job_id, status="error", error=str(exc))
    finally:
        # Outermost: cancelling while QUEUED raises inside _await_build_slot,
        # before any inner block runs, and the ticket would otherwise sit in the
        # queue forever holding a slot nobody uses.
        BUILDS.release(ticket)


def _run_connect_job(job_id: str, ticket, a: str, b: str,
                     context_a: str, context_b: str, ask: str,
                     owner_name: str) -> None:
    """Route search. Opens its own DB session: this runs on a worker thread,
    long after the request-scoped session from Depends() has been closed."""
    from .route_engine import run_route
    from .db import BoardsSessionLocal

    db = BoardsSessionLocal()
    try:
        _wrapper_job(job_id, ticket, run_route, a, b, ask, context_a, context_b,
                     db=db, operator_name=owner_name)
    finally:
        db.close()


def _run_discover_job(job_id: str, ticket, name: str) -> None:
    from .route_engine import run_discover
    _wrapper_job(job_id, ticket, run_discover, name)


@app.post("/connect")
def connect(req: dict, request: Request) -> dict:
    """Kick off a route search between two people; poll GET /jobs/{job_id}.

    Body: {"person_a", "person_b", "ask"?, "context_a"?, "context_b"?}

    `ask` is optional and defaults to "N/A". Supplying it sharpens the result —
    each hop is judged on whether that person would plausibly make THIS request
    of the next — but a blank ask no longer blocks a run; it simply means the
    hops are judged without a particular request in mind.

    `owner_name` says who is ASKING. It gates one thing: when it matches
    `person_a`, that person's imported connections are offered to the model as
    first-degree ties for the first hop. Tag anyone else as the origin and the
    tool is not offered at all — those contacts are not their connections.

    `depth` is accepted and ignored; there is one search budget now
    (route_engine.MAX_SEARCHES) rather than a dial."""
    a = _str_field(req, "person_a")
    b = _str_field(req, "person_b")
    ask = _str_field(req, "ask") or "N/A"
    context_a = _str_field(req, "context_a")
    context_b = _str_field(req, "context_b")
    if not a or not b:
        raise HTTPException(status_code=400, detail="person_a and person_b required")
    if not context_a or not context_b:
        raise HTTPException(
            status_code=400,
            detail="context_a and context_b are required — a bare name resolves "
                   "to the wrong person and the search fails silently, citing real "
                   "sources about somebody else",
        )
    return _start_build_job(
        request, "connect", _run_connect_job,
        (a, b, context_a, context_b, ask, _str_field(req, "owner_name")))


@app.post("/discover")
def discover(req: dict, request: Request) -> dict:
    """Map one person's closest verified professional connections.

    `depth` is accepted and ignored — see /connect."""
    name = _str_field(req, "person_name")
    if not name:
        raise HTTPException(status_code=400, detail="person_name required")
    return _start_build_job(request, "discover", _run_discover_job, (name,))


def _get_owned_board(db: Session, board_id: str, owner_id: str) -> Board:
    b = db.get(Board, board_id)
    if b is None or b.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="board not found")
    return b


def _page_dict(p: BoardPage) -> dict:
    return {"id": p.id, "name": p.name, "position": p.position, "elements": p.elements or {}}


def _board_pages(db: Session, board_id: str) -> list[BoardPage]:
    return list(db.execute(
        select(BoardPage).where(BoardPage.board_id == board_id).order_by(BoardPage.position.asc())
    ).scalars())


def _board_summary(b: Board, seq: int, pages: list[BoardPage]) -> dict:
    nodes = sum(len((p.elements or {}).get("nodes") or []) for p in pages)
    edges = sum(len((p.elements or {}).get("edges") or []) for p in pages)
    return {
        "id": b.id, "seq": seq, "name": b.name, "status": b.status or "active",
        "created_at": b.created_at, "target_name": b.target_name, "target_org": b.target_org,
        "pages": len(pages), "nodes": nodes, "edges": edges,
        # first page's elements power the boards-list minimap preview
        "preview_elements": (pages[0].elements or {}) if pages else {},
    }


@app.post("/boards")
def create_board(req: dict, owner_id: str = Depends(_owner_id),
                  db: Session = Depends(get_boards_db)) -> dict:
    name = _str_field(req, "name")
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    board = Board(
        owner_id=owner_id, name=name,
        target_name=_str_field(req, "target_name") or None,
        target_org=_str_field(req, "target_org") or None,
    )
    db.add(board)
    db.flush()
    page = BoardPage(board_id=board.id, name="Page 1", position=0, elements={})
    db.add(page)
    db.commit()
    # This board was just created, so it's necessarily the newest for this
    # owner -- a plain (indexed) owner_id count already equals its seq,
    # without the unindexed `created_at <=` string range-scan.
    seq = db.query(Board).filter(Board.owner_id == owner_id).count()
    return _board_summary(board, seq, [page])


@app.get("/boards")
def list_boards(owner_id: str = Depends(_owner_id), db: Session = Depends(get_boards_db)) -> list:
    rows = list(db.execute(
        select(Board).where(Board.owner_id == owner_id).order_by(Board.created_at.asc())
    ).scalars())
    # One query for all boards' pages instead of one per board (N+1).
    pages_by_board: dict[str, list] = defaultdict(list)
    if rows:
        board_ids = [b.id for b in rows]
        for p in db.execute(
            select(BoardPage).where(BoardPage.board_id.in_(board_ids))
            .order_by(BoardPage.board_id, BoardPage.position.asc())
        ).scalars():
            pages_by_board[p.board_id].append(p)
    summaries = [_board_summary(b, i + 1, pages_by_board.get(b.id, []))
                 for i, b in enumerate(rows)]
    summaries.reverse()  # newest first
    return summaries


@app.get("/boards/{board_id}")
def get_board(board_id: str, owner_id: str = Depends(_owner_id),
              db: Session = Depends(get_boards_db)) -> dict:
    b = _get_owned_board(db, board_id, owner_id)
    pages = _board_pages(db, board_id)
    return {"id": b.id, "name": b.name, "status": b.status or "active",
            "target_name": b.target_name, "target_org": b.target_org,
            "created_at": b.created_at, "pages": [_page_dict(p) for p in pages]}


@app.patch("/boards/{board_id}")
def update_board(board_id: str, req: dict, owner_id: str = Depends(_owner_id),
                  db: Session = Depends(get_boards_db)) -> dict:
    b = _get_owned_board(db, board_id, owner_id)
    if "status" in req:
        if req["status"] not in ("active", "archived"):
            raise HTTPException(status_code=400, detail="status must be 'active' or 'archived'")
        b.status = req["status"]
    if "name" in req:
        new_name = _str_field(req, "name")
        if new_name:
            b.name = new_name
    if "target_name" in req:
        b.target_name = _str_field(req, "target_name") or None
    if "target_org" in req:
        b.target_org = _str_field(req, "target_org") or None
    db.commit()
    seq = db.query(Board).filter(
        Board.owner_id == owner_id, Board.created_at <= b.created_at
    ).count()
    return _board_summary(b, seq, _board_pages(db, board_id))


@app.delete("/boards/{board_id}")
def delete_board(board_id: str, owner_id: str = Depends(_owner_id),
                  db: Session = Depends(get_boards_db)) -> dict:
    b = _get_owned_board(db, board_id, owner_id)
    db.query(BoardPage).filter(BoardPage.board_id == b.id).delete()
    db.delete(b)
    db.commit()
    return {"deleted": board_id}


# ---- Pages ------------------------------------------------------------------
@app.post("/boards/{board_id}/pages")
def create_page(board_id: str, req: dict, owner_id: str = Depends(_owner_id),
                 db: Session = Depends(get_boards_db)) -> dict:
    _get_owned_board(db, board_id, owner_id)
    existing = _board_pages(db, board_id)
    name = _str_field(req, "name") or f"Page {len(existing) + 1}"
    page = BoardPage(board_id=board_id, name=name, position=len(existing), elements={})
    db.add(page)
    db.commit()
    return _page_dict(page)


@app.patch("/boards/{board_id}/pages/{page_id}")
def update_page(board_id: str, page_id: str, req: dict, owner_id: str = Depends(_owner_id),
                 db: Session = Depends(get_boards_db)) -> dict:
    _get_owned_board(db, board_id, owner_id)
    page = db.get(BoardPage, page_id)
    if page is None or page.board_id != board_id:
        raise HTTPException(status_code=404, detail="page not found")
    if "name" in req:
        new_name = _str_field(req, "name")
        if new_name:
            page.name = new_name
    if "elements" in req:
        page.elements = req.get("elements") or {}
    db.commit()
    return _page_dict(page)


@app.delete("/boards/{board_id}/pages/{page_id}")
def delete_page(board_id: str, page_id: str, owner_id: str = Depends(_owner_id),
                 db: Session = Depends(get_boards_db)) -> dict:
    _get_owned_board(db, board_id, owner_id)
    pages = _board_pages(db, board_id)
    if len(pages) <= 1:
        raise HTTPException(status_code=400, detail="a board must keep at least one page")
    page = db.get(BoardPage, page_id)
    if page is None or page.board_id != board_id:
        raise HTTPException(status_code=404, detail="page not found")
    db.delete(page)
    db.commit()
    return {"deleted": page_id}


# --- static frontend (mounted last so it never shadows the API routes) ------


# ---------------------------------------------------------------------------
# Stubs for UI features this build does not implement.
#
# The shipped board UI calls these on boot and from panels that are still on
# screen. They belonged to the deleted engine — a local relationship graph built
# from imported contacts, and the matching/clique/path analysis over it. None of
# it is involved in finding a route now.
#
# They answer with a valid empty result rather than 404 so the UI renders its
# own "nothing here" states instead of throwing. Each returns `supported: false`
# so a caller can tell an empty answer from an absent feature — a silent [] would
# read as "you have no contacts" rather than "this build has no contacts
# feature", and that difference matters to whoever is debugging.
# ---------------------------------------------------------------------------
_UNSUPPORTED = {"supported": False,
                "detail": "not available in this build — routing is Claude + web search only"}


@app.get("/people")
def list_people() -> list:
    return []


def _require_owner_name(owner_name: str) -> str:
    """Reject an owner-less read or delete.

    These rows are somebody's first-degree connections and carry real personal
    data — names, employers, emails, LinkedIn URLs — for people who never used
    this app. Defaulting the scope to "everyone" turned a missing query
    parameter into a dump of every operator's contacts, and on the delete side
    into a wipe of the whole table. The upload path already refuses an owner-less
    write; read and delete now agree with it."""
    if not owner_name.strip():
        raise HTTPException(
            status_code=400,
            detail="owner_name is required — contacts are only usable as "
                   "first-degree ties for the person who uploaded them",
        )
    return owner_name


@app.get("/network/profiles")
def list_network_profiles(owner_name: str = "",
                          db: Session = Depends(get_boards_db)) -> list:
    """The operator's imported contacts.

    `owner_name` scopes the read and is required — see `_require_owner_name`."""
    _require_owner_name(owner_name)
    return [contacts.to_profile_dict(c) for c in contacts.list_for_owner(db, owner_name)]


@app.delete("/network/profiles")
def delete_network_profiles(owner_name: str = "",
                            db: Session = Depends(get_boards_db)) -> dict:
    """Delete one operator's contacts. `owner_name` is required: without it this
    deleted every row in the table rather than that person's."""
    _require_owner_name(owner_name)
    return {"deleted": contacts.delete_for_owner(db, owner_name)}


@app.post("/network/upload")
async def upload_network(file: UploadFile = File(...),
                         owner_name: str = Form(""),
                         db: Session = Depends(get_boards_db)) -> dict:
    """Import a LinkedIn Connections.csv.

    `owner_name` is required, and the requirement is the point: a contact list
    with nobody attached to it cannot be used as anyone's first-degree ties, so
    accepting one would store rows that routing must then ignore."""
    if not owner_name.strip():
        raise HTTPException(
            status_code=400,
            detail="owner_name is required — contacts are only usable as "
                   "first-degree ties for the person who uploaded them",
        )
    raw = await file.read()
    try:
        rows = contacts.parse_linkedin_csv(raw)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"could not parse CSV: {exc}")
    if not rows:
        raise HTTPException(
            status_code=400,
            detail="no connections found in that file — expected LinkedIn's "
                   "Connections.csv, which has First Name / Last Name columns",
        )
    return {"ingested": contacts.ingest(db, owner_name, rows, source="linkedin_csv"),
            "parsed": len(rows)}


@app.post("/network/contacts/import")
def import_contacts(req: dict, db: Session = Depends(get_boards_db)) -> dict:
    """Import contacts as JSON rows (the vCard / phone-contacts path)."""
    owner_name = _str_field(req, "owner_name")
    if not owner_name:
        raise HTTPException(status_code=400, detail="owner_name is required")
    rows = req.get("contacts")
    if not isinstance(rows, list) or not rows:
        raise HTTPException(status_code=400, detail="contacts must be a non-empty list")
    return {"ingested": contacts.ingest(db, owner_name, rows, source="vcard")}


@app.post("/network/profiles/backfill-graph-edges")
def backfill_edges(req: dict) -> dict:
    # Nothing to backfill: contacts are read directly at route time, not
    # projected into a graph that could fall behind them.
    return {"created": 0, "supported": False,
            "detail": "not needed — contacts are read live at route time"}


@app.post("/network/cliques")
def network_cliques(req: dict) -> dict:
    return {"cliques": [], "count": 0, **_UNSUPPORTED}


@app.post("/match/{target_person_id}")
def match_target(target_person_id: str) -> dict:
    return {"matches": [], "count": 0, **_UNSUPPORTED}


@app.get("/candidate-paths")
def candidate_paths() -> dict:
    return {"paths": [], "count": 0, **_UNSUPPORTED}


_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/ui", StaticFiles(directory=_STATIC_DIR, html=True), name="ui")
