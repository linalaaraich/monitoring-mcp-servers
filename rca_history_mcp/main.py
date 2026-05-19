"""RCA History MCP Server (:8095)

Read-only bridge giving the LLM institutional memory of past investigations.
Queries the SQLite RCA history database (shared volume from triage service).
Enables the LLM to learn from previous incidents: 'has this alert fired
before?', 'what was the root cause last time?', 'is this a recurring pattern?'
"""

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import aiosqlite
from fastapi import FastAPI, Query
from shared.metrics import install_metrics

logging.basicConfig(level=logging.INFO, format='{"time":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}')
logger = logging.getLogger(__name__)

RCA_DB_PATH = os.getenv("RCA_DB_PATH", "/var/lib/triage-service/rca_history.db")

db: aiosqlite.Connection | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db
    db = await aiosqlite.connect(RCA_DB_PATH)
    db.row_factory = aiosqlite.Row
    logger.info("Connected to RCA history DB at %s", RCA_DB_PATH)
    yield
    await db.close()


app = FastAPI(title="RCA History MCP Server", version="0.1.0", lifespan=lifespan)
install_metrics(app)


@app.get("/health")
async def health():
    reachable = False
    try:
        cursor = await db.execute("SELECT COUNT(*) as cnt FROM rca_history")
        row = await cursor.fetchone()
        reachable = row is not None
    except Exception:
        pass
    return {"status": "healthy" if reachable else "degraded", "database_reachable": reachable}


@app.get("/tools/get_recent_rcas")
async def get_recent_rcas(hours: int = Query(24, ge=1, le=168, description="Hours to look back")):
    """Return RCA decisions from the last N hours.

    Gives the LLM a view of recent alert activity — are alerts clustering?
    Is the system in a degraded state with multiple firing alerts?
    """
    since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    cursor = await db.execute(
        "SELECT * FROM rca_history WHERE timestamp > ? ORDER BY timestamp DESC",
        (since,),
    )
    rows = await cursor.fetchall()
    records = [dict(row) for row in rows]

    return {
        "hours": hours,
        "count": len(records),
        "records": records,
    }


@app.get("/tools/search_rcas")
async def search_rcas(
    alert_name: str = Query(..., description="Alert name to search for"),
    days: int = Query(7, ge=1, le=30, description="Days to look back"),
):
    """Find past RCA decisions for a specific alert name.

    The LLM uses this to answer: 'Has HighP95Latency fired before? What was
    the root cause? Was it a real issue or noise?' This institutional memory
    prevents the LLM from starting each investigation from scratch.
    """
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    cursor = await db.execute(
        "SELECT * FROM rca_history WHERE alert_name = ? AND timestamp > ? ORDER BY timestamp DESC",
        (alert_name, since),
    )
    rows = await cursor.fetchall()
    records = [dict(row) for row in rows]

    escalated = sum(1 for r in records if r.get("action_taken") == "emailed")
    dismissed = sum(1 for r in records if r.get("action_taken") == "suppressed")

    return {
        "alert_name": alert_name,
        "days": days,
        "total_occurrences": len(records),
        "escalated": escalated,
        "dismissed": dismissed,
        "records": records,
    }


@app.get("/tools/get_rca_detail")
async def get_rca_detail(rca_id: str = Query(..., description="RCA record UUID")):
    """Return full detail of a specific past RCA investigation.

    Includes the LLM's full reasoning, root cause analysis, evidence used,
    and actions taken. The LLM can reference this to avoid repeating work
    or to compare current symptoms with previous incidents.
    """
    cursor = await db.execute("SELECT * FROM rca_history WHERE id = ?", (rca_id,))
    row = await cursor.fetchone()
    if not row:
        return {"error": "RCA record not found", "rca_id": rca_id}
    return dict(row)


@app.get("/tools/get_alert_frequency")
async def get_alert_frequency(
    alert_name: str = Query(..., description="Alert name"),
    days: int = Query(7, ge=1, le=30, description="Days to analyze"),
):
    """Return firing frequency and pattern for a specific alert.

    Helps the LLM understand: is this a one-off alert or a recurring problem?
    Frequent firing of the same alert suggests a persistent underlying issue
    rather than a transient spike.
    """
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()

    cursor = await db.execute(
        "SELECT COUNT(*) as cnt FROM rca_history WHERE alert_name = ? AND timestamp > ?",
        (alert_name, since),
    )
    count_row = await cursor.fetchone()
    count = count_row["cnt"] if count_row else 0

    cursor = await db.execute(
        "SELECT timestamp, llm_verdict, action_taken FROM rca_history WHERE alert_name = ? AND timestamp > ? ORDER BY timestamp DESC",
        (alert_name, since),
    )
    rows = await cursor.fetchall()

    verdicts = {}
    for row in rows:
        v = row["llm_verdict"] or "unknown"
        verdicts[v] = verdicts.get(v, 0) + 1

    last_seen = rows[0]["timestamp"] if rows else None

    return {
        "alert_name": alert_name,
        "days": days,
        "total_firings": count,
        "last_seen": last_seen,
        "verdict_distribution": verdicts,
        "pattern": _describe_pattern(count, days),
    }


def _describe_pattern(count: int, days: int) -> str:
    if count == 0:
        return "never fired in this period"
    avg_per_day = count / days
    if avg_per_day < 0.2:
        return "rare — fires occasionally"
    elif avg_per_day < 1:
        return "intermittent — fires a few times per week"
    elif avg_per_day < 5:
        return "frequent — fires daily"
    else:
        return "chronic — fires multiple times per day, likely a persistent issue"


# -----------------------------------------------------------------------------
# S3-HF-06 (shipped 2026-05-19) — quality-rated tools
#
# Two new tools the LLM (or its bounded-agency retry path) can call to pull
# *good* prior decisions as positive exemplars and *operator-overridden*
# prior decisions as anti-exemplars. Without these the LLM either sees no
# history at all (no learning) or sees indiscriminate history including the
# RCAs operators rated wrong (negative learning). Quality-filtered history is
# the precondition for Sprint-4's curated-RAG retrieval (US-3.3 / US-3.4) —
# the curation job will read from these tools to build the curated YAML.
#
# Quality ordering (matches app/rca_store._classify_rca_quality):
#   actionable    = best  (concrete cause + evidence + actions)
#   data_starved  = middle (RCA explicitly hedged "insufficient data")
#   needs_review  = worst (no actions, no evidence — human needed)
# -----------------------------------------------------------------------------

_QUALITY_RANK = {"actionable": 2, "data_starved": 1, "needs_review": 0}
_DEFAULT_MIN_QUALITY = "actionable"
_VALID_QUALITIES = ("actionable", "data_starved", "needs_review")


@app.get("/tools/get_similar_decisions")
async def get_similar_decisions(
    alert_name: str = Query(..., description="Alert name to match"),
    affected_service: str | None = Query(
        None,
        description="Service to match. If omitted, alert_name-only match "
                    "(useful for archetype-level pattern search)."
    ),
    min_quality: str = Query(
        _DEFAULT_MIN_QUALITY,
        description="Minimum rca_quality (actionable | data_starved | needs_review). "
                    "Results returned at this quality OR HIGHER per the ordering "
                    "actionable > data_starved > needs_review."
    ),
    days: int = Query(30, ge=1, le=180, description="Days to look back"),
    limit: int = Query(5, ge=1, le=20, description="Max records to return"),
):
    """Return high-quality past decisions for an alert (optionally + service).

    Use case: the LLM's bounded-agency retry path wants positive exemplars to
    learn from — *prior RCAs we judged 'actionable' for this exact alert
    pattern*. Filtering by min_quality means the retrieved context never
    teaches the LLM to repeat a previous low-quality answer.

    Returns the records ordered newest-first. Empty list is a normal answer
    when the pattern is new or all prior decisions were low-quality.
    """
    if min_quality not in _VALID_QUALITIES:
        return {
            "error": f"min_quality must be one of {_VALID_QUALITIES}",
            "received": min_quality,
        }
    min_rank = _QUALITY_RANK[min_quality]
    allowed_qualities = [q for q, r in _QUALITY_RANK.items() if r >= min_rank]

    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    placeholders = ",".join("?" * len(allowed_qualities))

    if affected_service:
        sql = (
            "SELECT * FROM rca_history "
            f"WHERE alert_name = ? AND affected_service = ? "
            f"  AND timestamp > ? AND rca_quality IN ({placeholders}) "
            "ORDER BY timestamp DESC LIMIT ?"
        )
        params = (alert_name, affected_service, since, *allowed_qualities, limit)
    else:
        sql = (
            "SELECT * FROM rca_history "
            f"WHERE alert_name = ? AND timestamp > ? "
            f"  AND rca_quality IN ({placeholders}) "
            "ORDER BY timestamp DESC LIMIT ?"
        )
        params = (alert_name, since, *allowed_qualities, limit)

    cursor = await db.execute(sql, params)
    rows = await cursor.fetchall()
    records = [dict(row) for row in rows]

    return {
        "alert_name": alert_name,
        "affected_service": affected_service,
        "min_quality": min_quality,
        "qualities_included": allowed_qualities,
        "days": days,
        "count": len(records),
        "records": records,
    }


@app.get("/tools/get_low_rated_examples_for_alert")
async def get_low_rated_examples_for_alert(
    alert_name: str = Query(..., description="Alert name to match"),
    days: int = Query(60, ge=1, le=180, description="Days to look back"),
    limit: int = Query(3, ge=1, le=10, description="Max records to return"),
):
    """Return past decisions for this alert that operators *overrode*.

    "Low-rated" is concrete here: there is a `feedback` row with
    feedback_type='override' linked to the decision, meaning an operator
    disagreed with the system's verdict strongly enough to flag it. These
    are anti-exemplars — RCAs the LLM should learn NOT to produce again.

    Returns the decision row joined with the operator's note. Use cases:
      1. Sprint-4 curated-RAG: emit these as `<bad-example>` blocks in the
         prompt so the LLM sees what to avoid.
      2. Diagnostic dashboards: surface "the system has been wrong N times
         on this alert in the last 60 days" to operators reviewing trends.

    Empty list means either no overrides exist for this alert (the system
    has been agreeing with operators) or no operators have used the
    feedback path. Both readings are informative — check absolute volume
    of decisions for this alert separately.
    """
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    cursor = await db.execute(
        """
        SELECT r.*, f.operator_note, f.created_at as feedback_created_at
        FROM rca_history r
        INNER JOIN feedback f ON f.decision_id = r.id
        WHERE r.alert_name = ?
          AND r.timestamp > ?
          AND f.feedback_type = 'override'
        ORDER BY f.created_at DESC
        LIMIT ?
        """,
        (alert_name, since, limit),
    )
    rows = await cursor.fetchall()
    records = [dict(row) for row in rows]

    return {
        "alert_name": alert_name,
        "days": days,
        "count": len(records),
        "records": records,
    }
