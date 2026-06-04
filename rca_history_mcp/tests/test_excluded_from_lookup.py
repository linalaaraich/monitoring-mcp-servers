"""Stage E (2026-06-04) — MCP-side excluded_from_lookup filter.

Mirrors the triage-side test_excluded_from_lookup module: every LLM-context
read endpoint on the rca_history MCP must filter out rows where
excluded_from_lookup=1, so the bounded-agency surface never feeds the LLM
a row the operator has marked as garbage.

Endpoints checked: get_recent_rcas, search_rcas, get_alert_frequency,
get_similar_decisions, list_feedback, get_low_rated_examples_for_alert.
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
    rca_quality TEXT,
    excluded_from_lookup INTEGER DEFAULT 0
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
    rating TEXT,
    verdict_was_right TEXT,
    action_was_right TEXT,
    actual_cause TEXT,
    tags TEXT,
    notes TEXT,
    rater TEXT,
    FOREIGN KEY(decision_id) REFERENCES rca_history(id),
    UNIQUE(decision_id, feedback_type)
)
"""


def _hours_ago(h: int) -> str:
    return (datetime.utcnow() - timedelta(hours=h)).isoformat()


def _seed(path: str) -> None:
    """Two decisions for the same (alert_name, service); one is excluded."""
    conn = sqlite3.connect(path)
    conn.executescript(_RCA_HISTORY_SCHEMA + ";\n" + _FEEDBACK_SCHEMA + ";")
    conn.executemany(
        "INSERT INTO rca_history (id, timestamp, alert_source, alert_name, "
        "alert_fingerprint, affected_service, severity, triage_decision, "
        "llm_verdict, llm_confidence, rca_report, llm_reasoning, action_taken, "
        "related_alerts, investigation_duration_ms, rca_quality, excluded_from_lookup) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("kept",     _hours_ago(1), "grafana", "HighP95Latency", "fpk", "kong",
             "warning", "processed", "ESCALATE", "0.85", "clean actionable rca", "reasoning",
             "emailed", None, 4200, "actionable", 0),
            ("excluded", _hours_ago(2), "grafana", "HighP95Latency", "fpe", "kong",
             "warning", "processed", "ESCALATE", "0.5",  "insufficient data hedge",
             "reasoning", "emailed", None, 800, "data_starved", 1),
        ],
    )
    # Operator override on the EXCLUDED row — must not surface via
    # get_low_rated_examples_for_alert (the quarantine wins).
    conn.execute(
        "INSERT INTO feedback (id, decision_id, feedback_type, operator_note, created_at, active_until) "
        "VALUES (?, ?, 'override', 'should not surface', ?, ?)",
        (str(uuid.uuid4()), "excluded", _hours_ago(2), _hours_ago(-72)),
    )
    # Rate feedback on the EXCLUDED row — must not surface via list_feedback.
    conn.execute(
        "INSERT INTO feedback (id, decision_id, feedback_type, operator_note, created_at, "
        "active_until, rating, verdict_was_right, actual_cause, rater) "
        "VALUES (?, ?, 'rate', NULL, ?, NULL, 'no', 'no', 'real cause was OOM', 'alice')",
        (str(uuid.uuid4()), "excluded", _hours_ago(2)),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "rca_history.db")
    _seed(db_path)
    monkeypatch.setattr(mcp_main, "RCA_DB_PATH", db_path)
    with TestClient(mcp_main.app) as c:
        yield c


def test_get_recent_rcas_skips_excluded(client):
    r = client.get("/tools/get_recent_rcas", params={"hours": 24})
    assert r.status_code == 200
    data = r.json()
    ids = sorted(rec["id"] for rec in data["records"])
    assert ids == ["kept"], "excluded row must not surface in get_recent_rcas"


def test_search_rcas_skips_excluded(client):
    r = client.get(
        "/tools/search_rcas",
        params={"alert_name": "HighP95Latency", "days": 7},
    )
    assert r.status_code == 200
    data = r.json()
    ids = sorted(rec["id"] for rec in data["records"])
    assert ids == ["kept"]
    assert data["total_occurrences"] == 1


def test_alert_frequency_skips_excluded(client):
    r = client.get(
        "/tools/get_alert_frequency",
        params={"alert_name": "HighP95Latency", "days": 7},
    )
    assert r.status_code == 200
    data = r.json()
    # Only the kept row counts toward the frequency.
    assert data["total_firings"] == 1


def test_similar_decisions_skips_excluded(client):
    r = client.get(
        "/tools/get_similar_decisions",
        params={
            "alert_name": "HighP95Latency",
            "affected_service": "kong",
            "min_quality": "data_starved",  # would otherwise INCLUDE the excluded row
            "days": 7,
            "limit": 10,
        },
    )
    assert r.status_code == 200
    data = r.json()
    ids = sorted(rec["id"] for rec in data["records"])
    assert ids == ["kept"], "data_starved excluded row must not surface"


def test_list_feedback_skips_excluded(client):
    r = client.get(
        "/tools/list_feedback",
        params={
            "alert_name": "HighP95Latency",
            "service": "kong",
            "days": 14,
            "limit": 10,
        },
    )
    assert r.status_code == 200
    data = r.json()
    # Both feedback rows point at the EXCLUDED decision — none must surface.
    assert data["count"] == 0
    assert data["records"] == []


def test_low_rated_examples_skips_excluded(client):
    r = client.get(
        "/tools/get_low_rated_examples_for_alert",
        params={"alert_name": "HighP95Latency", "days": 60, "limit": 10},
    )
    assert r.status_code == 200
    data = r.json()
    # The override on the EXCLUDED row must not leak — quarantine wins.
    assert data["count"] == 0
