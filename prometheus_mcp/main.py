"""Prometheus MCP Server (:8091)

Read-only bridge giving the LLM access to Prometheus metrics.
Proxies PromQL queries to the Prometheus HTTP API on the monitoring VM.
"""

import logging
import os

import httpx
from fastapi import FastAPI, Query

logging.basicConfig(level=logging.INFO, format='{"time":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}')
logger = logging.getLogger(__name__)

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")

app = FastAPI(title="Prometheus MCP Server", version="0.1.0")
client = httpx.AsyncClient(timeout=15)


@app.get("/health")
async def health():
    reachable = False
    try:
        resp = await client.get(f"{PROMETHEUS_URL}/-/ready")
        reachable = resp.status_code == 200
    except Exception:
        pass
    return {"status": "healthy" if reachable else "degraded", "prometheus_reachable": reachable}


@app.get("/tools/query_instant")
async def query_instant(promql: str = Query(..., description="PromQL expression")):
    """Execute an instant PromQL query and return the current value.

    Use this for point-in-time checks like 'is this target up right now?' or
    'what is the current CPU usage?'.
    """
    resp = await client.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": promql})
    resp.raise_for_status()
    data = resp.json()
    return {
        "status": data.get("status"),
        "result_type": data.get("data", {}).get("resultType"),
        "result": data.get("data", {}).get("result", []),
    }


@app.get("/tools/query_range")
async def query_range(
    promql: str = Query(..., description="PromQL expression"),
    start: str = Query("15m", description="Start time (e.g. '15m' for 15 minutes ago, or ISO timestamp)"),
    end: str = Query("now", description="End time (e.g. 'now' or ISO timestamp)"),
    step: str = Query("60s", description="Query resolution step"),
):
    """Execute a range PromQL query and return a time series.

    Use this to see how a metric changed over a time window — essential for
    spotting trends, spikes, or correlating with alert firing times.
    """
    params = {"query": promql, "step": step}

    # Handle relative time strings
    if start.endswith("m"):
        import time
        minutes = int(start.rstrip("m"))
        params["start"] = str(time.time() - minutes * 60)
    else:
        params["start"] = start

    if end == "now":
        import time
        params["end"] = str(time.time())
    else:
        params["end"] = end

    resp = await client.get(f"{PROMETHEUS_URL}/api/v1/query_range", params=params)
    resp.raise_for_status()
    data = resp.json()
    return {
        "status": data.get("status"),
        "result_type": data.get("data", {}).get("resultType"),
        "result": data.get("data", {}).get("result", []),
    }


@app.get("/tools/get_alerts")
async def get_alerts():
    """Return all currently firing alerts from Prometheus.

    Shows which alert rules are active right now, useful for understanding
    the broader context — are multiple alerts firing simultaneously?
    """
    resp = await client.get(f"{PROMETHEUS_URL}/api/v1/alerts")
    resp.raise_for_status()
    data = resp.json()
    alerts = data.get("data", {}).get("alerts", [])
    return {
        "firing_count": sum(1 for a in alerts if a.get("state") == "firing"),
        "alerts": [
            {
                "alertname": a.get("labels", {}).get("alertname"),
                "state": a.get("state"),
                "severity": a.get("labels", {}).get("severity"),
                "instance": a.get("labels", {}).get("instance"),
                "summary": a.get("annotations", {}).get("summary"),
                "activeAt": a.get("activeAt"),
            }
            for a in alerts
        ],
    }


@app.get("/tools/get_targets")
async def get_targets():
    """Return all Prometheus scrape targets and their health status.

    Quickly identifies which services are UP or DOWN — a DOWN target
    correlates strongly with service-affecting alerts.
    """
    resp = await client.get(f"{PROMETHEUS_URL}/api/v1/targets")
    resp.raise_for_status()
    data = resp.json()
    active = data.get("data", {}).get("activeTargets", [])
    return {
        "total": len(active),
        "up": sum(1 for t in active if t.get("health") == "up"),
        "down": sum(1 for t in active if t.get("health") == "down"),
        "targets": [
            {
                "job": t.get("labels", {}).get("job"),
                "instance": t.get("labels", {}).get("instance"),
                "health": t.get("health"),
                "lastScrape": t.get("lastScrape"),
                "lastError": t.get("lastError"),
            }
            for t in active
        ],
    }
