"""Loki MCP Server (:8092)

Read-only bridge giving the LLM access to Loki log queries.
Proxies LogQL queries to the Loki HTTP API on the monitoring VM.
Response limit enforced: max 50 log lines per call.
"""

import logging
import os
import time

import httpx
from fastapi import FastAPI, Query
from shared.metrics import install_metrics

logging.basicConfig(level=logging.INFO, format='{"time":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}')
logger = logging.getLogger(__name__)

LOKI_URL = os.getenv("LOKI_URL", "http://loki:3100")
MAX_LINES = 50

app = FastAPI(title="Loki MCP Server", version="0.1.0")
install_metrics(app)
client = httpx.AsyncClient(timeout=15)


@app.get("/health")
async def health():
    reachable = False
    try:
        resp = await client.get(f"{LOKI_URL}/ready")
        reachable = resp.status_code == 200
    except Exception:
        pass
    return {"status": "healthy" if reachable else "degraded", "loki_reachable": reachable}


@app.get("/tools/query_logs")
async def query_logs(
    logql: str = Query(..., description='LogQL expression, e.g. {service_name="spring-boot"}'),
    start: str = Query("15m", description="Start time (e.g. '15m' ago or ISO timestamp)"),
    end: str = Query("now", description="End time"),
    limit: int = Query(50, ge=1, le=50, description="Max log lines (capped at 50)"),
):
    """Search logs matching a LogQL query within a time range.

    Returns up to 50 log lines. Use label matchers to narrow by service,
    and pipe expressions to filter content (e.g. |= "error" or |~ "trace_id=.*").
    """
    effective_limit = min(limit, MAX_LINES)
    params = {"query": logql, "limit": effective_limit, "direction": "backward"}

    now = time.time()
    if start.endswith("m"):
        minutes = int(start.rstrip("m"))
        params["start"] = str(int((now - minutes * 60) * 1e9))
    else:
        params["start"] = start

    if end == "now":
        params["end"] = str(int(now * 1e9))
    else:
        params["end"] = end

    resp = await client.get(f"{LOKI_URL}/loki/api/v1/query_range", params=params)
    resp.raise_for_status()
    data = resp.json()

    lines = []
    for stream in data.get("data", {}).get("result", []):
        labels = stream.get("stream", {})
        for ts, line in stream.get("values", []):
            lines.append(line)
            if len(lines) >= effective_limit:
                break
        if len(lines) >= effective_limit:
            break

    return {
        "lines": lines,
        "count": len(lines),
        "limit_applied": effective_limit,
    }


@app.get("/tools/get_label_values")
async def get_label_values(
    label: str = Query(..., description="Label name, e.g. 'service_name' or 'host_name'"),
):
    """Return all unique values for a given log label.

    Useful for discovering which services, hosts, or namespaces are
    producing logs — helps the LLM narrow its search.
    """
    resp = await client.get(f"{LOKI_URL}/loki/api/v1/label/{label}/values")
    resp.raise_for_status()
    data = resp.json()
    values = data.get("data", [])
    return {"label": label, "values": values, "count": len(values)}


@app.get("/tools/get_log_volume")
async def get_log_volume(
    logql: str = Query(..., description='LogQL selector, e.g. {service_name="spring-boot"}'),
    start: str = Query("15m", description="Start time"),
    end: str = Query("now", description="End time"),
):
    """Return log line counts over a time range for the given selector.

    Shows whether log volume spiked or dropped — a sudden increase in
    error logs correlates with incidents; a drop may indicate a service crash.
    """
    now = time.time()
    params = {"query": f'count_over_time({logql}[1m])', "step": "60s"}

    if start.endswith("m"):
        minutes = int(start.rstrip("m"))
        params["start"] = str(int((now - minutes * 60) * 1e9))
    else:
        params["start"] = start

    if end == "now":
        params["end"] = str(int(now * 1e9))
    else:
        params["end"] = end

    resp = await client.get(f"{LOKI_URL}/loki/api/v1/query_range", params=params)
    resp.raise_for_status()
    data = resp.json()

    series = []
    for result in data.get("data", {}).get("result", []):
        labels = result.get("metric", {})
        values = result.get("values", [])
        total = sum(float(v) for _, v in values)
        series.append({"labels": labels, "total_lines": int(total), "datapoints": len(values)})

    return {"series": series}
