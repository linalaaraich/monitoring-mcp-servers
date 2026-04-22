"""Drain3 MCP Server (:8094)

Exposes the triage service's internal Drain3 state to the LLM.
Queries the triage service's /drain3/stats endpoint and provides
tool-style endpoints the LLM can call during investigation.
"""

import logging
import os

import httpx
from fastapi import FastAPI, Query
from shared.metrics import install_metrics

logging.basicConfig(level=logging.INFO, format='{"time":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}')
logger = logging.getLogger(__name__)

TRIAGE_URL = os.getenv("TRIAGE_SERVICE_URL", "http://triage-service:8090")

app = FastAPI(title="Drain3 MCP Server", version="0.1.0")
install_metrics(app)
client = httpx.AsyncClient(timeout=10)


@app.get("/health")
async def health():
    reachable = False
    try:
        resp = await client.get(f"{TRIAGE_URL}/health")
        reachable = resp.status_code == 200
    except Exception:
        pass
    return {
        "status": "healthy" if reachable else "degraded",
        "triage_service_reachable": reachable,
    }


@app.get("/tools/get_clusters")
async def get_clusters():
    """Return all known log template clusters with match counts.

    The LLM uses this to understand what log patterns are normal (high match
    count) versus novel (low match count). High-count templates are routine;
    low-count or newly created templates may indicate emerging issues.
    """
    resp = await client.get(f"{TRIAGE_URL}/drain3/stats")
    resp.raise_for_status()
    stats = resp.json()

    clusters = []
    for i, template in enumerate(stats.get("top_new_patterns", [])):
        clusters.append({
            "template": template,
            "rank": i + 1,
            "category": "recent",
        })

    return {
        "total_clusters": stats.get("total_clusters", 0),
        "clusters": clusters,
        "total_lines_processed": stats.get("total_lines_processed", 0),
    }


@app.get("/tools/get_anomaly_rate")
async def get_anomaly_rate():
    """Return the current ratio of anomalous to total log lines.

    A rate near 0.0 means almost all logs match known templates (healthy).
    A rate above 0.1 means >10% of logs are novel patterns (investigate).
    A rate near 0.0 for extended periods after a deploy could mean Drain3
    needs reseeding (baseline drift).
    """
    resp = await client.get(f"{TRIAGE_URL}/drain3/stats")
    resp.raise_for_status()
    stats = resp.json()

    return {
        "anomaly_rate": stats.get("recent_anomaly_rate", 0.0),
        "total_anomalies": stats.get("total_anomalies", 0),
        "total_lines_processed": stats.get("total_lines_processed", 0),
        "interpretation": _interpret_rate(stats.get("recent_anomaly_rate", 0.0)),
    }


@app.get("/tools/match_log")
async def match_log(log_line: str = Query(..., description="Log line to match against Drain3 templates")):
    """Check if a specific log line matches a known template or is anomalous.

    The LLM uses this to drill into specific log lines from the context —
    for example, to check whether an error message it found in Loki is a
    known pattern or something never seen before.
    """
    # We query the triage service's drain3 analyze endpoint
    # Since the triage service embeds Drain3, we ask it to classify the line
    resp = await client.get(f"{TRIAGE_URL}/drain3/stats")
    resp.raise_for_status()
    stats = resp.json()

    # Check against known templates
    total_clusters = stats.get("total_clusters", 0)
    top_patterns = stats.get("top_new_patterns", [])

    matched = False
    matched_template = None
    for template in top_patterns:
        # Simple substring containment heuristic
        if _template_matches(template, log_line):
            matched = True
            matched_template = template
            break

    return {
        "log_line": log_line,
        "is_anomalous": not matched,
        "matched_template": matched_template,
        "total_clusters": total_clusters,
        "note": "Anomalous lines are novel patterns not matching any known template" if not matched else "Line matches a known log template",
    }


@app.get("/tools/get_baseline_info")
async def get_baseline_info():
    """Return information about the Drain3 baseline state.

    Tells the LLM how mature the anomaly detection baseline is — a freshly
    seeded baseline with few clusters may produce false positives, while a
    well-trained baseline with hundreds of clusters is more reliable.
    """
    resp = await client.get(f"{TRIAGE_URL}/drain3/stats")
    resp.raise_for_status()
    stats = resp.json()

    total = stats.get("total_clusters", 0)
    lines = stats.get("total_lines_processed", 0)

    if total < 50:
        maturity = "immature — baseline still learning, expect false positives"
    elif total < 200:
        maturity = "developing — baseline has reasonable coverage"
    else:
        maturity = "mature — baseline well-trained with broad template coverage"

    return {
        "total_clusters": total,
        "total_lines_processed": lines,
        "anomaly_rate": stats.get("recent_anomaly_rate", 0.0),
        "maturity": maturity,
    }


def _interpret_rate(rate: float) -> str:
    if rate < 0.01:
        return "very low — almost all logs match known templates"
    elif rate < 0.05:
        return "low — most logs are routine, a few novel patterns"
    elif rate < 0.15:
        return "moderate — notable number of novel log patterns"
    else:
        return "high — many logs are anomalous, possible incident or baseline drift"


def _template_matches(template: str, log_line: str) -> bool:
    """Simple heuristic: check if the template's non-variable parts appear in the log line."""
    parts = [p for p in template.split() if not p.startswith("<") and len(p) > 2]
    if not parts:
        return False
    return sum(1 for p in parts if p in log_line) / len(parts) > 0.6
