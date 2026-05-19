"""S3-HF-06 tests — quality-rated tools on rca_history_mcp.

Seeds a temporary SQLite file synchronously, points the MCP's
RCA_DB_PATH at it, and uses FastAPI TestClient as a context manager
so the lifespan opens its own aiosqlite connection naturally.

If the upstream schema changes (new columns on rca_history or feedback),
the fixture SQL here must change with it. The schemas live in
`monitoring-triage-service/app/rca_store.py`.
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import rca_history_mcp.main as mcp_main


_RCA_HISTORY_SCHEMA = """
CREATE TABLE rca_history (
    id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    alert_source TEXT NOT NULL,
    alert_name TEXT NOT NULL,
    alert_fingerprint TEXT,
    affected_service TEXT,
    severity TEXT,
    triage_decision TEXT NOT NULL,
    llm_verdict TEXT,
    llm_confidence TEXT,
    rca_report TEXT,
    llm_reasoning TEXT,
    action_taken TEXT NOT NULL,
    related_alerts TEXT,
    investigation_duration_ms INTEGER DEFAULT 0,
    rca_quality TEXT
)
"""

_FEEDBACK_SCHEMA = """
CREATE TABLE feedback (
    id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    feedback_type TEXT NOT NULL,
    operator_note TEXT,
    created_at TEXT NOT NULL,
    active_until TEXT,
    FOREIGN KEY(decision_id) REFERENCES rca_history(id),
    UNIQUE(decision_id, feedback_type)
)
"""


def _hours_ago(h: int) -> str:
    return (datetime.utcnow() - timedelta(hours=h)).isoformat()


def _seed(path: str) -> None:
    """Seed the database synchronously. Records (newest first):

      d_act_1  alert=HighP95Latency  svc=kong         quality=actionable   1h ago
      d_act_2  alert=HighP95Latency  svc=spring-boot  quality=actionable   3h ago  (OVERRIDDEN)
      d_ds_1   alert=HighP95Latency  svc=kong         quality=data_starved 5h ago
      d_nr_1   alert=HighP95Latency  svc=kong         quality=needs_review 8h ago
      d_oom_1  alert=HighMemoryUsage svc=spring-boot  quality=actionable   2h ago
      d_old_1  alert=HighP95Latency  svc=kong         quality=actionable   45 days ago (outside default 30-day window)
    """
    conn = sqlite3.connect(path)
    conn.executescript(_RCA_HISTORY_SCHEMA + ";\n" + _FEEDBACK_SCHEMA + ";")
    rows = [
        ("d_act_1", _hours_ago(1),   "grafana", "HighP95Latency",  "fp1", "kong",        "warning", "processed", "ESCALATE",     "0.85", "kong p95 elevated", "rca prose", "emailed",   None, 4200, "actionable"),
        ("d_act_2", _hours_ago(3),   "grafana", "HighP95Latency",  "fp2", "spring-boot", "warning", "processed", "ESCALATE",     "0.91", "spring lock",       "rca prose", "emailed",   None, 5800, "actionable"),
        ("d_ds_1",  _hours_ago(5),   "grafana", "HighP95Latency",  "fp3", "kong",        "warning", "processed", "INCONCLUSIVE", "0.4",  "hedged rca",        "reasoning", "suppressed",None, 1200, "data_starved"),
        ("d_nr_1",  _hours_ago(8),   "grafana", "HighP95Latency",  "fp4", "kong",        "warning", "processed", "ESCALATE",     "0.5",  "thin rca",          "reasoning", "emailed",   None,  800, "needs_review"),
        ("d_oom_1", _hours_ago(2),   "grafana", "HighMemoryUsage", "fp5", "spring-boot", "critical","processed", "ESCALATE",     "0.92", "jvm oom",           "reasoning", "emailed",   None, 6100, "actionable"),
        ("d_old_1", (datetime.utcnow() - timedelta(days=45)).isoformat(),
                                       "grafana", "HighP95Latency",  "fp6", "kong",        "warning", "processed", "ESCALATE",     "0.88", "old", "old", "emailed", None, 4000, "actionable"),
    ]
    conn.executemany(
        "INSERT INTO rca_history VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    # One override on d_act_2 — operator disagreed with that ESCALATE.
    conn.execute(
        "INSERT INTO feedback VALUES (?,?,?,?,?,?)",
        (str(uuid.uuid4()), "d_act_2", "override",
         "operator says this was a flake", _hours_ago(2), None),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Return a TestClient backed by a freshly-seeded SQLite file.

    Using TestClient as a context manager triggers the lifespan handler,
    which opens its own aiosqlite connection — no manual asyncio plumbing
    needed in the test body.
    """
    db_path = str(tmp_path / "rca.db")
    _seed(db_path)
    monkeypatch.setattr(mcp_main, "RCA_DB_PATH", db_path)
    with TestClient(mcp_main.app) as c:
        yield c


# ------------------------------------------------------------------------
# get_similar_decisions
# ------------------------------------------------------------------------

def test_similar_decisions_actionable_only_returns_just_actionables(client):
    """Default min_quality=actionable — only the two recent actionable
    HighP95Latency rows should come back (the 45-day-old one is outside
    the 30-day window)."""
    r = client.get("/tools/get_similar_decisions", params={"alert_name": "HighP95Latency"})
    assert r.status_code == 200
    data = r.json()
    assert data["min_quality"] == "actionable"
    assert data["qualities_included"] == ["actionable"]
    ids = [rec["id"] for rec in data["records"]]
    assert "d_act_1" in ids
    assert "d_act_2" in ids
    assert "d_ds_1" not in ids
    assert "d_nr_1" not in ids
    assert "d_old_1" not in ids


def test_similar_decisions_filters_by_service(client):
    """affected_service=kong should drop d_act_2 (which is spring-boot)."""
    r = client.get(
        "/tools/get_similar_decisions",
        params={"alert_name": "HighP95Latency", "affected_service": "kong"},
    )
    data = r.json()
    ids = [rec["id"] for rec in data["records"]]
    assert ids == ["d_act_1"]


def test_similar_decisions_min_quality_data_starved_includes_higher(client):
    """min_quality=data_starved means actionable + data_starved both qualify,
    but needs_review is excluded."""
    r = client.get(
        "/tools/get_similar_decisions",
        params={"alert_name": "HighP95Latency", "min_quality": "data_starved"},
    )
    data = r.json()
    assert set(data["qualities_included"]) == {"actionable", "data_starved"}
    ids = [rec["id"] for rec in data["records"]]
    assert "d_act_1" in ids and "d_act_2" in ids and "d_ds_1" in ids
    assert "d_nr_1" not in ids


def test_similar_decisions_ordered_newest_first(client):
    """Records must come back ordered timestamp DESC — newest first."""
    r = client.get(
        "/tools/get_similar_decisions",
        params={"alert_name": "HighP95Latency", "min_quality": "needs_review"},
    )
    data = r.json()
    ids = [rec["id"] for rec in data["records"]]
    # Expected order: d_act_1 (1h), d_act_2 (3h), d_ds_1 (5h), d_nr_1 (8h)
    assert ids == ["d_act_1", "d_act_2", "d_ds_1", "d_nr_1"]


def test_similar_decisions_rejects_invalid_min_quality(client):
    r = client.get(
        "/tools/get_similar_decisions",
        params={"alert_name": "HighP95Latency", "min_quality": "magnificent"},
    )
    data = r.json()
    assert "error" in data
    assert "magnificent" in data["received"]


def test_similar_decisions_respects_limit(client):
    r = client.get(
        "/tools/get_similar_decisions",
        params={"alert_name": "HighP95Latency", "min_quality": "needs_review", "limit": 2},
    )
    data = r.json()
    assert len(data["records"]) == 2


# ------------------------------------------------------------------------
# get_low_rated_examples_for_alert
# ------------------------------------------------------------------------

def test_low_rated_returns_overridden_decisions(client):
    """Only d_act_2 has an override feedback row in the fixture."""
    r = client.get(
        "/tools/get_low_rated_examples_for_alert",
        params={"alert_name": "HighP95Latency"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    rec = data["records"][0]
    assert rec["id"] == "d_act_2"
    assert rec["operator_note"] == "operator says this was a flake"
    assert "feedback_created_at" in rec


def test_low_rated_returns_empty_when_no_overrides(client):
    """HighMemoryUsage has decisions but no overrides — empty list."""
    r = client.get(
        "/tools/get_low_rated_examples_for_alert",
        params={"alert_name": "HighMemoryUsage"},
    )
    data = r.json()
    assert data["count"] == 0
    assert data["records"] == []
