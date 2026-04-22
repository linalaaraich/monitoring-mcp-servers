"""Jaeger MCP Server (:8093)

Read-only bridge giving the LLM access to distributed traces.
Proxies queries to the Jaeger HTTP API on the monitoring VM.
Enables the LLM to find error spans, analyze latency, and correlate
trace_ids with Loki log lines.
"""

import logging
import os
import time

import httpx
from fastapi import FastAPI, Query
from shared.metrics import install_metrics

logging.basicConfig(level=logging.INFO, format='{"time":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}')
logger = logging.getLogger(__name__)

JAEGER_URL = os.getenv("JAEGER_URL", "http://jaeger:16686")

app = FastAPI(title="Jaeger MCP Server", version="0.1.0")
install_metrics(app)
client = httpx.AsyncClient(timeout=15)


@app.get("/health")
async def health():
    reachable = False
    try:
        resp = await client.get(f"{JAEGER_URL}/api/services")
        reachable = resp.status_code == 200
    except Exception:
        pass
    return {"status": "healthy" if reachable else "degraded", "jaeger_reachable": reachable}


@app.get("/tools/find_traces")
async def find_traces(
    service: str = Query(..., description="Service name, e.g. 'react-springboot-app'"),
    operation: str = Query(None, description="Operation name filter (optional)"),
    start: str = Query("15m", description="Lookback start (e.g. '15m' or microsecond timestamp)"),
    end: str = Query("now", description="Lookback end"),
    limit: int = Query(20, ge=1, le=100, description="Max traces to return"),
    tags: str = Query(None, description='Tag filter as JSON, e.g. {"error":"true","http.status_code":"500"}'),
):
    """Search for traces by service, time window, and optional tags.

    Use this to find traces during the alert window — especially error traces
    or high-latency traces that correlate with the alert. The trace_id in
    results can be used with get_trace() for full detail, or cross-referenced
    with Loki logs containing the same trace_id.
    """
    now_us = int(time.time() * 1_000_000)
    params = {"service": service, "limit": limit}

    # Jaeger expects microseconds as an INTEGER string, not a float.
    # Previously "start=<float>.0" made Jaeger return 400 Bad Request.
    if start.endswith("m"):
        minutes = int(start.rstrip("m"))
        params["start"] = str(now_us - minutes * 60 * 1_000_000)
    else:
        params["start"] = start

    if end == "now":
        params["end"] = str(now_us)
    else:
        params["end"] = end

    if operation:
        params["operation"] = operation

    if tags:
        import json
        try:
            tag_dict = json.loads(tags)
            for k, v in tag_dict.items():
                params[f"tags"] = json.dumps(tag_dict)
                break
        except json.JSONDecodeError:
            pass

    resp = await client.get(f"{JAEGER_URL}/api/traces", params=params)
    resp.raise_for_status()
    data = resp.json()

    traces = []
    for trace in data.get("data", []):
        spans = trace.get("spans", [])
        trace_id = trace.get("traceID", "")

        root_span = spans[0] if spans else {}
        error_spans = [s for s in spans if any(t.get("key") == "error" and t.get("value") is True for t in s.get("tags", []))]

        traces.append({
            "trace_id": trace_id,
            "span_count": len(spans),
            "duration_ms": round(root_span.get("duration", 0) / 1000, 1),
            "operation": root_span.get("operationName", ""),
            "error_span_count": len(error_spans),
            "has_errors": len(error_spans) > 0,
        })

    return {"traces": traces, "count": len(traces)}


@app.get("/tools/get_trace")
async def get_trace(trace_id: str = Query(..., description="Full trace ID (hex string)")):
    """Get full detail for a specific trace including all spans.

    Returns the complete span tree: each span's name, duration, status code,
    tags, and parent-child relationships. Use this after find_traces() to
    drill into a specific problematic trace. Cross-reference the trace_id
    with Loki to find log lines from the same request.
    """
    resp = await client.get(f"{JAEGER_URL}/api/traces/{trace_id}")
    resp.raise_for_status()
    data = resp.json()

    traces = data.get("data", [])
    if not traces:
        return {"error": "trace not found", "trace_id": trace_id}

    trace = traces[0]
    processes = trace.get("processes", {})
    spans = []

    for span in trace.get("spans", []):
        proc = processes.get(span.get("processID", ""), {})
        tags = {t["key"]: t["value"] for t in span.get("tags", [])}
        spans.append({
            "span_id": span.get("spanID"),
            "operation": span.get("operationName"),
            "service": proc.get("serviceName", "unknown"),
            "duration_ms": round(span.get("duration", 0) / 1000, 1),
            "status_code": tags.get("http.status_code"),
            "error": tags.get("error", False),
            "tags": tags,
            "parent_span_id": next(
                (r.get("spanID") for r in span.get("references", []) if r.get("refType") == "CHILD_OF"),
                None,
            ),
        })

    return {
        "trace_id": trace_id,
        "span_count": len(spans),
        "spans": spans,
    }


@app.get("/tools/get_services")
async def get_services():
    """List all services that have produced traces.

    Quick way to see which instrumented services are active — should include
    at least 'react-springboot-app' and 'kong-gateway'.
    """
    resp = await client.get(f"{JAEGER_URL}/api/services")
    resp.raise_for_status()
    data = resp.json()
    services = data.get("data", [])
    return {"services": services, "count": len(services)}


@app.get("/tools/get_operations")
async def get_operations(service: str = Query(..., description="Service name")):
    """List all operations (endpoints/methods) for a service.

    Shows which API endpoints or internal operations are traced — helps
    narrow the search to specific operations like 'GET /api/employees'.
    """
    resp = await client.get(f"{JAEGER_URL}/api/services/{service}/operations")
    resp.raise_for_status()
    data = resp.json()
    operations = data.get("data", [])
    return {"service": service, "operations": operations, "count": len(operations)}
