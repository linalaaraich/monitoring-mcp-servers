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
"""
from __future__ import annotations

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse


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
