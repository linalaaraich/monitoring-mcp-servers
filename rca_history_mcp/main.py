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
