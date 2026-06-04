"""MCP Issue-6 (HIGH) — upstream-error → HTTP-status mapping.

shared/errors.install_error_handlers maps the upstream httpx client's error
types onto the statuses the triage client expects:

  * upstream HTTP status  < 500  → 422 Unprocessable Entity  (bad query)
  * upstream HTTP status >= 500  → 502 Bad Gateway            (upstream outage)
  * connection / timeout error   → 502 Bad Gateway            (upstream down)
  * healthy 200                  → 200 (untouched)

These tests exercise the four httpx-backed MCP servers (Prometheus, Loki,
Jaeger, Drain3) through their real tool endpoints, with the upstream httpx
AsyncClient mocked so no real upstream is contacted. The mapping lives in the
shared handler, so one representative endpoint per server proves it for that
server.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

import prometheus_mcp.main as prom_main
import loki_mcp.main as loki_main
import jaeger_mcp.main as jaeger_main
import drain3_mcp.main as drain3_main


# (module, attr-holding-the-client, endpoint, query-params) per server.
# The endpoint is a real tool route that makes a single upstream GET.
_SERVERS = [
    (prom_main, "/tools/query_instant", {"promql": "up"}),
    (loki_main, "/tools/query_logs", {"logql": '{service_name="x"}'}),
    (jaeger_main, "/tools/get_services", {}),
    (drain3_main, "/tools/get_clusters", {}),
]

_IDS = ["prometheus", "loki", "jaeger", "drain3"]


class _FakeResponse:
    """Minimal stand-in for httpx.Response used by raise_for_status()."""

    def __init__(self, status_code: int):
        self.status_code = status_code

    def json(self):
        # Only reached on the healthy path. Empty Prometheus/Loki/Jaeger-shaped
        # payload that every endpoint's parser tolerates.
        return {"status": "success", "data": {"result": [], "resultType": "vector"}}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("GET", "http://upstream/x"),
                response=httpx.Response(
                    self.status_code, request=httpx.Request("GET", "http://upstream/x")
                ),
            )


def _install_fake_client(monkeypatch, module, behaviour):
    """Replace module.client with a fake whose .get() runs `behaviour()`."""

    class _FakeClient:
        async def get(self, *args, **kwargs):
            return behaviour()

    monkeypatch.setattr(module, "client", _FakeClient())


def _client(module):
    return TestClient(module.app)


@pytest.mark.parametrize("module,endpoint,params", _SERVERS, ids=_IDS)
def test_upstream_404_maps_to_422(monkeypatch, module, endpoint, params):
    """An upstream 4xx (bad query) becomes 422, not 500."""
    _install_fake_client(monkeypatch, module, lambda: _FakeResponse(404))
    resp = _client(module).get(endpoint, params=params)
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["detail"] == "upstream query rejected"
    assert body["upstream_status"] == 404


@pytest.mark.parametrize("module,endpoint,params", _SERVERS, ids=_IDS)
def test_upstream_503_maps_to_502(monkeypatch, module, endpoint, params):
    """An upstream 5xx (upstream outage) becomes 502, not 500."""
    _install_fake_client(monkeypatch, module, lambda: _FakeResponse(503))
    resp = _client(module).get(endpoint, params=params)
    assert resp.status_code == 502, resp.text
    body = resp.json()
    assert body["detail"] == "upstream returned an error"
    assert body["upstream_status"] == 503


@pytest.mark.parametrize("module,endpoint,params", _SERVERS, ids=_IDS)
def test_connection_error_maps_to_502(monkeypatch, module, endpoint, params):
    """A connection/timeout error (upstream unreachable) becomes 502."""

    def _boom():
        raise httpx.ConnectError(
            "connection refused", request=httpx.Request("GET", "http://upstream/x")
        )

    _install_fake_client(monkeypatch, module, _boom)
    resp = _client(module).get(endpoint, params=params)
    assert resp.status_code == 502, resp.text
    assert resp.json()["detail"] == "upstream unreachable"


@pytest.mark.parametrize("module,endpoint,params", _SERVERS, ids=_IDS)
def test_healthy_call_is_200(monkeypatch, module, endpoint, params):
    """A healthy upstream response is left untouched (200)."""
    _install_fake_client(monkeypatch, module, lambda: _FakeResponse(200))
    resp = _client(module).get(endpoint, params=params)
    assert resp.status_code == 200, resp.text


def test_timeout_exception_maps_to_502(monkeypatch):
    """ReadTimeout (a RequestError subclass) also maps to 502."""

    def _boom():
        raise httpx.ReadTimeout(
            "timed out", request=httpx.Request("GET", "http://upstream/x")
        )

    _install_fake_client(monkeypatch, prom_main, _boom)
    resp = _client(prom_main).get("/tools/query_instant", params={"promql": "up"})
    assert resp.status_code == 502
    assert resp.json()["detail"] == "upstream unreachable"
