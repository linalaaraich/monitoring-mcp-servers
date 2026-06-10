"""Shared upstream-error → HTTP-status mapping for every MCP bridge server.

MCP Issue-6 (HIGH): every MCP server proxies to an upstream (Prometheus /
Loki / Jaeger over HTTP, or the triage service for Drain3). When an upstream
call fails, FastAPI previously let the raw exception bubble into a generic
HTTP 500. The triage client distinguishes 4xx (bad request — the *query* was
wrong, client's fault, don't treat as an outage) from 5xx (upstream/transient
— a genuine MCP outage). A blanket 500 defeats that distinction.

This module installs FastAPI exception handlers that translate the upstream
HTTP client's error types into the right status:

  * upstream returned a status < 500 (e.g. 400 / 404 / 422 — a bad PromQL /
    LogQL query, unknown trace id, malformed params) → **422 Unprocessable
    Entity**. The platform is fine; the QUERY was bad.

  * upstream returned a status >= 500, OR we could not reach the upstream at
    all (connection refused, DNS failure, timeout) → **502 Bad Gateway**. This
    is a genuine outage the triage client should treat as an MCP being down.

  * everything else (healthy responses) is left untouched.

The response body is JSON with a clear ``detail`` field, plus ``upstream_status``
on the 422 path so a human / the client can see which upstream code drove the
mapping. No new data sources are introduced — this is pure error mapping, so
the MCP-only data-access invariant is preserved.

Each server calls :func:`install_error_handlers(app)` once, right after
constructing its FastAPI app (next to ``install_metrics``).

M-1 (2026-06-10 audit cycle): rca_history is the one MCP whose "upstream" is
NOT an httpx call — it reads SQLite via aiosqlite (``aiosqlite.Error`` IS
``sqlite3.Error``). It previously skipped this module entirely, so an
unexpected DB exception (locked DB, schema drift) bubbled to a raw 500 which
the triage client mis-read as a source outage. It now ALSO calls
:func:`install_sqlite_error_handlers`, which applies the same 422/502
contract to the DB-backed path:

  * ``sqlite3.OperationalError`` with a transient/infra message ("database is
    locked", "unable to open database file", disk I/O…) → **502** — the DB
    (the upstream) is unavailable; treat as a source outage.

  * any other ``sqlite3.OperationalError`` ("no such column/table", SQL
    syntax — a query/schema problem, not an outage) → **422** — the platform
    is up; the QUERY was bad, fix the args instead of abandoning the source.

  * any other ``sqlite3.Error`` (corruption, integrity, programming…) → **502**.
"""
from __future__ import annotations

import sqlite3

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse

# sqlite3.OperationalError messages that mean "the DB is unavailable right
# now" (infra/transient → 502) rather than "your SQL/args were wrong" (422).
_SQLITE_TRANSIENT_MARKERS = (
    "locked",            # database is locked / database table is locked
    "busy",              # database is busy
    "unable to open",    # unable to open database file (volume gone)
    "disk i/o",          # disk I/O error
    "readonly",          # attempt to write a readonly database
    "out of memory",
)


def install_error_handlers(app) -> None:
    """Install httpx upstream-error → 422/502 handlers on the given app."""

    @app.exception_handler(httpx.HTTPStatusError)
    async def _on_status_error(request: Request, exc: httpx.HTTPStatusError):
        # The upstream answered, but with an error status. < 500 means the
        # request/query was rejected (client's fault → 422); >= 500 means the
        # upstream itself failed (outage → 502).
        upstream_status = exc.response.status_code
        if upstream_status < 500:
            return JSONResponse(
                status_code=422,
                content={
                    "detail": "upstream query rejected",
                    "upstream_status": upstream_status,
                },
            )
        return JSONResponse(
            status_code=502,
            content={
                "detail": "upstream returned an error",
                "upstream_status": upstream_status,
            },
        )

    @app.exception_handler(httpx.RequestError)
    async def _on_request_error(request: Request, exc: httpx.RequestError):
        # Connection refused, DNS failure, timeout, read error — we never got
        # a usable response from the upstream. Genuine outage → 502.
        return JSONResponse(
            status_code=502,
            content={"detail": "upstream unreachable"},
        )


def install_sqlite_error_handlers(app) -> None:
    """Install sqlite/aiosqlite upstream-error → 422/502 handlers (M-1).

    For the DB-backed MCP (rca_history) whose upstream is a SQLite file, not
    an HTTP service. Mirrors :func:`install_error_handlers` semantics so the
    triage client's 4xx-bad-query / 5xx-outage contract holds for every MCP.
    aiosqlite re-raises the stdlib ``sqlite3`` exception types, so handling
    ``sqlite3.*`` covers both. Starlette picks the most specific registered
    class in the exception's MRO, so OperationalError wins over Error.
    """

    @app.exception_handler(sqlite3.OperationalError)
    async def _on_sqlite_operational(request: Request, exc: sqlite3.OperationalError):
        msg = str(exc).lower()
        if any(marker in msg for marker in _SQLITE_TRANSIENT_MARKERS):
            # DB locked / file unreachable / disk I/O — the upstream (the DB)
            # is genuinely unavailable → 502 outage.
            return JSONResponse(
                status_code=502,
                content={"detail": "database unavailable", "sqlite_error": str(exc)},
            )
        # "no such column", "no such table", SQL syntax — the query/schema was
        # wrong, the platform is fine → 422 bad query.
        return JSONResponse(
            status_code=422,
            content={"detail": "database query rejected", "sqlite_error": str(exc)},
        )

    @app.exception_handler(sqlite3.Error)
    async def _on_sqlite_error(request: Request, exc: sqlite3.Error):
        # Anything else from the sqlite layer (corruption, integrity,
        # programming errors) — not a client-fixable query problem → 502.
        return JSONResponse(
            status_code=502,
            content={"detail": "database error", "sqlite_error": str(exc)},
        )
