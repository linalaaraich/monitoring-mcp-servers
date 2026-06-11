"""Deploy MCP (:8096) — /tools/recent_deploys + /tools/last_deploy.

Fix F (2026-06-11): the platform previously had NO deploy data source, so a
deploy-as-cause RCA could only ever be fabricated. This bridge derives
rollouts from kube-state-metrics series already in Prometheus.

Locks in:
  - a "deploy" = the NEWEST ReplicaSet of a deployment created inside the
    window; stale RS rows (which persist forever in kube-state-metrics) are
    deduped away, surfacing only the latest rollout per deployment
  - prior-RS name + how long the prior version ran ("rolled from X")
  - newest-first ordering across deployments
  - empty list when nothing rolled in the window (grounded negative)
  - namespace/service scoping, owner-series mapping with prefix fallback
  - 422 on a malformed window (shared M-1 contract: bad params are the
    client's fault, not an outage)
  - last_deploy: latest rollout regardless of window, 404-shaped detail
    when the deployment has no RS history
"""
from __future__ import annotations

import time

import httpx
import pytest
from fastapi.testclient import TestClient

import deploy_mcp.main as mcp_main

NOW = time.time()


def _series(metric: dict, value: float) -> dict:
    return {"metric": metric, "value": [NOW, str(value)]}


def _created(ns: str, rs: str, ts: float) -> dict:
    return _series(
        {"__name__": "kube_replicaset_created", "exported_namespace": ns, "replicaset": rs},
        ts,
    )


def _owner(ns: str, rs: str, deployment: str) -> dict:
    return _series(
        {
            "__name__": "kube_replicaset_owner",
            "exported_namespace": ns,
            "replicaset": rs,
            "owner_kind": "Deployment",
            "owner_name": deployment,
            "owner_is_controller": "true",
        },
        1,
    )


# The live shape (verified 2026-06-11): ad rolled 20 min ago (prior RS from
# 6 days back), image-provider rolled 5 min ago (prior 90 min back),
# currency has only an OLD RS (no rollout in any recent window).
_CREATED = [
    _created("otel-demo", "ad-69f7649c7d", NOW - 20 * 60),
    _created("otel-demo", "ad-d678c8d65", NOW - 6 * 86400),
    _created("otel-demo", "image-provider-646fb587f7", NOW - 5 * 60),
    _created("otel-demo", "image-provider-7cc6d988d4", NOW - 110 * 60),
    _created("otel-demo", "currency-84d7dcb6b", NOW - 6 * 86400),
]
_OWNERS = [
    _owner("otel-demo", "ad-69f7649c7d", "ad"),
    _owner("otel-demo", "ad-d678c8d65", "ad"),
    _owner("otel-demo", "image-provider-646fb587f7", "image-provider"),
    _owner("otel-demo", "image-provider-7cc6d988d4", "image-provider"),
    _owner("otel-demo", "currency-84d7dcb6b", "currency"),
]


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("GET", "http://prometheus/x"),
                response=httpx.Response(
                    self.status_code, request=httpx.Request("GET", "http://prometheus/x")
                ),
            )


def _prom_payload(result: list[dict]) -> dict:
    return {"status": "success", "data": {"resultType": "vector", "result": result}}


def _install_fake_prom(monkeypatch, created=_CREATED, owners=_OWNERS):
    """Fake the upstream Prometheus: route by which metric the query names."""

    class _FakeClient:
        async def get(self, url, params=None):
            query = (params or {}).get("query", "")
            if "kube_replicaset_created" in query:
                rows = created
            elif "kube_replicaset_owner" in query:
                rows = owners
            else:
                rows = []
            # honour the namespace selector the server builds
            if 'exported_namespace="' in query:
                ns = query.split('exported_namespace="')[1].split('"')[0]
                rows = [r for r in rows if r["metric"]["exported_namespace"] == ns]
            return _FakeResponse(_prom_payload(rows))

    monkeypatch.setattr(mcp_main, "client", _FakeClient())


@pytest.fixture
def api(monkeypatch):
    _install_fake_prom(monkeypatch)
    return TestClient(mcp_main.app)


# ---------------------------------------------------------------------------
# /tools/recent_deploys
# ---------------------------------------------------------------------------

def test_recent_deploys_dedupes_to_newest_rs_and_sorts_newest_first(api):
    resp = api.get("/tools/recent_deploys", params={"namespace": "otel-demo", "window": "2h"})
    assert resp.status_code == 200, resp.text
    deploys = resp.json()
    # currency's only RS is 6 days old — excluded; ad + image-provider once each
    assert [d["deployment"] for d in deploys] == ["image-provider", "ad"]
    ip, ad = deploys
    assert ip["replicaset"] == "image-provider-646fb587f7"
    assert ip["namespace"] == "otel-demo"
    assert 4 <= ip["age_minutes"] <= 6
    assert ip["prior_replicaset"] == "image-provider-7cc6d988d4"
    assert 100 <= ip["prior_replicaset_age_minutes"] <= 110
    assert ad["replicaset"] == "ad-69f7649c7d"
    assert ad["prior_replicaset"] == "ad-d678c8d65"
    assert ad["rollout_time_iso"].startswith("20")  # ISO timestamp present


def test_recent_deploys_service_scoping(api):
    deploys = api.get(
        "/tools/recent_deploys",
        params={"namespace": "otel-demo", "service": "ad", "window": "2h"},
    ).json()
    assert len(deploys) == 1
    assert deploys[0]["deployment"] == "ad"


def test_recent_deploys_empty_window_is_grounded_negative(api):
    # 1-minute window: nothing rolled that recently → [] (NOT an error).
    resp = api.get("/tools/recent_deploys", params={"namespace": "otel-demo", "window": "1m"})
    assert resp.status_code == 200
    assert resp.json() == []


def test_recent_deploys_old_rollouts_visible_with_wide_window(api):
    deploys = api.get(
        "/tools/recent_deploys", params={"namespace": "otel-demo", "window": "7d"}
    ).json()
    assert [d["deployment"] for d in deploys] == ["image-provider", "ad", "currency"]
    # currency never re-rolled → no prior RS fields
    assert "prior_replicaset" not in deploys[-1]


def test_recent_deploys_bad_window_is_422(api):
    for bad in ("2hours", "", "-2h", "2", "h"):
        resp = api.get("/tools/recent_deploys", params={"window": bad})
        assert resp.status_code == 422, f"window={bad!r} -> {resp.status_code}"
        assert "window" in resp.json()["detail"]


def test_recent_deploys_injection_shaped_namespace_is_422(api):
    resp = api.get(
        "/tools/recent_deploys",
        params={"namespace": 'otel-demo"}or{x="', "window": "2h"},
    )
    assert resp.status_code == 422


def test_owner_fallback_strips_pod_template_hash(monkeypatch):
    # No owner series at all → deployment derived from the RS name prefix.
    _install_fake_prom(monkeypatch, owners=[])
    api = TestClient(mcp_main.app)
    deploys = api.get(
        "/tools/recent_deploys", params={"namespace": "otel-demo", "window": "2h"}
    ).json()
    assert {d["deployment"] for d in deploys} == {"image-provider", "ad"}


# ---------------------------------------------------------------------------
# /tools/last_deploy
# ---------------------------------------------------------------------------

def test_last_deploy_returns_latest_rollout(api):
    resp = api.get("/tools/last_deploy", params={"namespace": "otel-demo", "service": "ad"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deployment"] == "ad"
    assert body["replicaset"] == "ad-69f7649c7d"
    assert body["prior_replicaset"] == "ad-d678c8d65"


def test_last_deploy_ignores_window_age(api):
    # currency last rolled 6 days ago — last_deploy still reports it.
    body = api.get("/tools/last_deploy", params={"service": "currency"}).json()
    assert body["deployment"] == "currency"
    assert body["age_minutes"] > 6 * 1440 - 5


def test_last_deploy_unknown_deployment_is_404_shaped(api):
    resp = api.get("/tools/last_deploy", params={"service": "nope"})
    assert resp.status_code == 404
    assert "no rollout history" in resp.json()["detail"]


def test_last_deploy_missing_service_is_422(api):
    assert api.get("/tools/last_deploy").status_code == 422


# ---------------------------------------------------------------------------
# upstream error contract (M-1) — shared handlers are installed
# ---------------------------------------------------------------------------

def test_upstream_unreachable_maps_to_502(monkeypatch):
    class _DownClient:
        async def get(self, url, params=None):
            raise httpx.ConnectError(
                "connection refused", request=httpx.Request("GET", "http://prometheus/x")
            )

    monkeypatch.setattr(mcp_main, "client", _DownClient())
    api = TestClient(mcp_main.app)
    resp = api.get("/tools/recent_deploys", params={"window": "2h"})
    assert resp.status_code == 502
    assert resp.json()["detail"] == "upstream unreachable"
