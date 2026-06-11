"""Deploy MCP Server (:8096)

Read-only bridge exposing DEPLOY/ROLLOUT events so "a recent deploy caused
this" becomes a groundable claim. Until this bridge existed the platform had
NO deploy data source — the response validator therefore rejects any
deploy-as-cause claim whose evidence cites nothing (the 2026-06-11
fabricated deploy-RCA). This bridge makes GROUNDED deploy claims possible in
both directions: a rollout shortly before an alert is real evidence, and an
empty result ("no deploys in the window") is a meaningful grounded NEGATIVE
that rules deploy-regression out.

No new cluster access is required: rollouts are derived from
kube-state-metrics series ALREADY scraped into Prometheus.

Signals (label names verified live 2026-06-11 — kube-state-metrics runs in
the `observability` namespace, so its own `namespace`/`pod` labels collide
with the scrape job's and arrive prefixed as `exported_namespace` etc.; the
`replicaset` / `owner_name` labels don't collide and keep their names):

  * ``kube_replicaset_created``  — value = RS creation epoch. Old RS rows
    PERSIST after a rollout, so we dedupe to the NEWEST RS per deployment;
    that newest-RS creation time IS the rollout timestamp.
  * ``kube_replicaset_owner{owner_kind="Deployment"}`` — maps a ReplicaSet
    to its owning Deployment. Fallback when the owner series is missing:
    the RS name is ``<deployment>-<pod-template-hash>``, so strip the last
    dash-segment.

A "deploy" = a ReplicaSet created within the lookback window that is the
newest RS of its deployment. The prior RS (the version rolled FROM) is
reported alongside so callers see "rolled from X after it ran N minutes".
"""

import logging
import os
import re
import time
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Query
from shared.errors import install_error_handlers
from shared.metrics import install_metrics

logging.basicConfig(level=logging.INFO, format='{"time":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}')
logger = logging.getLogger(__name__)

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")

app = FastAPI(title="Deploy MCP Server", version="0.1.0")
install_metrics(app)
install_error_handlers(app)
client = httpx.AsyncClient(timeout=15)

# Lookback window grammar: <int><unit>, unit in s/m/h/d (e.g. "2h", "30m").
_WINDOW_RE = re.compile(r"^(\d+)(s|m|h|d)$")
_WINDOW_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# PromQL label values are double-quoted strings; refuse anything that could
# break out of the quotes (quote/backslash/newline). Same read-only posture
# as the sibling bridges — params select, they never inject.
_LABEL_VALUE_RE = re.compile(r'^[^"\\\n]*$')


def _parse_window_seconds(window: str) -> int:
    """Parse a '2h' / '30m' style lookback window to seconds, or 422."""
    m = _WINDOW_RE.match(window or "")
    if not m:
        raise HTTPException(
            status_code=422,
            detail=f"invalid window {window!r} — expected <int><unit> with unit s/m/h/d, e.g. '2h'",
        )
    return int(m.group(1)) * _WINDOW_UNIT_SECONDS[m.group(2)]


def _label_selector(namespace: str) -> str:
    """Build the (optional) namespace selector for the kube-state series."""
    if not namespace:
        return ""
    if not _LABEL_VALUE_RE.match(namespace):
        raise HTTPException(status_code=422, detail="invalid namespace value")
    return f'exported_namespace="{namespace}"'


async def _query_instant(promql: str) -> list[dict]:
    """Run an instant PromQL query, return the result series list."""
    resp = await client.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": promql})
    resp.raise_for_status()
    return resp.json().get("data", {}).get("result", [])


def _deployment_of(replicaset: str) -> str:
    """Fallback deployment-name derivation: RS = <deployment>-<hash>."""
    return replicaset.rsplit("-", 1)[0] if "-" in replicaset else replicaset


async def _gather_rollouts(namespace: str, service: str) -> dict[tuple[str, str], list[dict]]:
    """Fetch RS-creation + RS-owner series and group them per deployment.

    Returns {(namespace, deployment): [rs_record, ...]} with each deployment's
    ReplicaSets sorted newest-first by creation time. ``service`` (deployment
    name) filtering is applied here so callers only pay for what they asked.
    """
    if service and not _LABEL_VALUE_RE.match(service):
        raise HTTPException(status_code=422, detail="invalid service value")
    ns_sel = _label_selector(namespace)
    created_q = f"kube_replicaset_created{{{ns_sel}}}"
    owner_sel = ", ".join(filter(None, ['owner_kind="Deployment"', ns_sel]))
    owner_q = f"kube_replicaset_owner{{{owner_sel}}}"

    created_series = await _query_instant(created_q)
    owner_series = await _query_instant(owner_q)

    # (namespace, replicaset) -> owning deployment
    owners: dict[tuple[str, str], str] = {}
    for s in owner_series:
        m = s.get("metric", {})
        rs = m.get("replicaset", "")
        ns = m.get("exported_namespace") or m.get("namespace") or ""
        if rs and m.get("owner_name"):
            owners[(ns, rs)] = m["owner_name"]

    # Dedupe duplicate scrapes of the same RS (keep max created), then group.
    created: dict[tuple[str, str], float] = {}
    for s in created_series:
        m = s.get("metric", {})
        rs = m.get("replicaset", "")
        ns = m.get("exported_namespace") or m.get("namespace") or ""
        try:
            ts = float(s.get("value", [None, None])[1])
        except (TypeError, ValueError):
            continue
        if rs:
            key = (ns, rs)
            created[key] = max(created.get(key, 0.0), ts)

    grouped: dict[tuple[str, str], list[dict]] = {}
    for (ns, rs), ts in created.items():
        deployment = owners.get((ns, rs)) or _deployment_of(rs)
        if service and deployment != service:
            continue
        grouped.setdefault((ns, deployment), []).append(
            {"replicaset": rs, "created": ts}
        )
    for records in grouped.values():
        records.sort(key=lambda r: r["created"], reverse=True)
    return grouped


def _rollout_record(ns: str, deployment: str, records: list[dict], now: float) -> dict:
    """Shape one deployment's newest RS (+ prior RS) into the API record."""
    newest = records[0]
    out = {
        "deployment": deployment,
        "namespace": ns,
        "rollout_time_iso": datetime.fromtimestamp(newest["created"], timezone.utc).isoformat(),
        "age_minutes": round((now - newest["created"]) / 60, 1),
        "replicaset": newest["replicaset"],
    }
    if len(records) > 1:
        prior = records[1]
        # How long the PREVIOUS version had been running when it was replaced
        # — lets callers say "rolled from X after N minutes".
        out["prior_replicaset"] = prior["replicaset"]
        out["prior_replicaset_age_minutes"] = round(
            (newest["created"] - prior["created"]) / 60, 1
        )
    return out


@app.get("/health")
async def health():
    reachable = False
    try:
        resp = await client.get(f"{PROMETHEUS_URL}/-/ready")
        reachable = resp.status_code == 200
    except Exception:
        pass
    return {"status": "healthy" if reachable else "degraded", "prometheus_reachable": reachable}


@app.get("/tools/recent_deploys")
async def recent_deploys(
    namespace: str = Query("", description="Kubernetes namespace to scope to (empty = all)"),
    service: str = Query("", description="Deployment name to scope to (empty = all)"),
    window: str = Query("2h", description="Lookback window, e.g. '2h', '30m', '24h'"),
):
    """List rollouts (newest ReplicaSet per deployment) within the window.

    A deployment appears at most ONCE — its latest rollout — sorted newest
    first. An EMPTY list is a meaningful grounded negative: no deploys
    happened in the window, so deploy-regression can be ruled out as a cause.
    """
    window_seconds = _parse_window_seconds(window)
    now = time.time()
    grouped = await _gather_rollouts(namespace, service)
    deploys = [
        _rollout_record(ns, deployment, records, now)
        for (ns, deployment), records in grouped.items()
        if records[0]["created"] >= now - window_seconds
    ]
    deploys.sort(key=lambda d: d["age_minutes"])
    return deploys


@app.get("/tools/last_deploy")
async def last_deploy(
    namespace: str = Query("", description="Kubernetes namespace to scope to (empty = all)"),
    service: str = Query(..., description="Deployment name (required)"),
):
    """Return the single latest rollout of a deployment, regardless of age.

    404 when the deployment has no ReplicaSet history at all (unknown
    deployment, or kube-state-metrics isn't scraping its namespace).
    """
    grouped = await _gather_rollouts(namespace, service)
    if not grouped:
        scope = f" in namespace '{namespace}'" if namespace else ""
        raise HTTPException(
            status_code=404,
            detail=f"no rollout history found for deployment '{service}'{scope}",
        )
    now = time.time()
    (ns, deployment), records = max(
        grouped.items(), key=lambda item: item[1][0]["created"]
    )
    return _rollout_record(ns, deployment, records, now)
