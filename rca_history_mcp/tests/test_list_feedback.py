"""Phase 6 (2026-06-03) — /tools/list_feedback endpoint.

Sister endpoint to /tools/get_low_rated_examples_for_alert: this one
returns ALL feedback rows (any feedback_type) for an alert_name +
optional service, ordered by feedback created_at DESC. The triage
service's bounded-agency uses it as an on-demand companion to the
proactive corrective_feedback_block built in app/pipeline.py.

Locks in:
  - JOIN feedback ↔ rca_history on decision_id
  - alert_name filter
  - optional service filter
  - days lookback filter
  - response shape (count + records list with the expected fields)
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import rca_history_mcp.main as mcp_main


# rca_history schema must match the canonical one in
# monitoring-triage-service/app/rca_store.py (additive migrations
# included so the feedback table carries the SF-7 columns).
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


def _days_ago(d: int) -> str:
    return (datetime.utcnow() - timedelta(days=d)).isoformat()


def _seed(path: str) -> None:
    """Seed three decisions + three feedback rows across two alerts/services.

    d1 HighP95Latency / kong         → feedback rate    2h ago   verdict_no
    d2 HighP95Latency / spring-boot  → feedback rate    1h ago   actual_cause
    d3 HighP95Latency / kong         → feedback confirm 20d ago  (outside 14d window)
    d4 OtherAlert / kong             → feedback rate    1h ago   (different alert)
    """
    conn = sqlite3.connect(path)
    conn.executescript(_RCA_HISTORY_SCHEMA + ";\n" + _FEEDBACK_SCHEMA + ";")
    dec_rows = [
        ("d1", _hours_ago(3), "grafana", "HighP95Latency",  "fp1", "kong",        "warning", "processed", "ESCALATE", "0.8", "rca1", "reasoning", "emailed",   None, 4200, "actionable"),
        ("d2", _hours_ago(2), "grafana", "HighP95Latency",  "fp2", "spring-boot", "warning", "processed", "DISMISS",  "0.6", "rca2", "reasoning", "suppressed", None, 1100, "actionable"),
        ("d3", _days_ago(25), "grafana", "HighP95Latency",  "fp3", "kong",        "warning", "processed", "ESCALATE", "0.7", "rca3", "reasoning", "emailed",   None, 3200, "actionable"),
        ("d4", _hours_ago(1), "grafana", "OtherAlert",      "fp4", "kong",        "warning", "processed", "DISMISS",  "0.5", "rca4", "reasoning", "suppressed", None, 900,  "actionable"),
    ]
    conn.executemany(
        "INSERT INTO rca_history (id, timestamp, alert_source, alert_name, "
        "alert_fingerprint, affected_service, severity, triage_decision, "
        "llm_verdict, llm_confidence, rca_report, llm_reasoning, action_taken, "
        "related_alerts, investigation_duration_ms, rca_quality) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        dec_rows,
    )
    # feedback rows. 13 columns: id, decision_id, feedback_type, operator_note,
    # created_at, active_until, rating, verdict_was_right, action_was_right,
    # actual_cause, tags, notes, rater
    fb_rows = [
        (str(uuid.uuid4()), "d1", "rate", None, _hours_ago(2),  None, "no",  "no",     None, None,        "[]", None, "alice"),
        (str(uuid.uuid4()), "d2", "rate", None, _hours_ago(1),  None, "partial", "maybe", "partial",
         "JVM heap leak, restart only masked it", "[]", "good catch overall", "bob"),
        (str(uuid.uuid4()), "d3", "confirm", "agreed",
         _days_ago(20), None, None, None, None, None, None, None, None),
        (str(uuid.uuid4()), "d4", "rate", None, _hours_ago(1), None,
         "yes", "yes", "yes", None, "[]", None, "carol"),
    ]
    conn.executemany(
        "INSERT INTO feedback VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        fb_rows,
    )
    conn.commit()
    conn.close()


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "rca.db")
    _seed(db_path)
    monkeypatch.setattr(mcp_main, "RCA_DB_PATH", db_path)
    with TestClient(mcp_main.app) as c:
        yield c


# ---------------------------------------------------------------------------
# Endpoint shape + filter behaviour
# ---------------------------------------------------------------------------


def test_list_feedback_filters_by_alert_name(client):
    """Only HighP95Latency rows within the 14d window come back."""
    r = client.get(
        "/tools/list_feedback",
        params={"alert_name": "HighP95Latency"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["alert_name"] == "HighP95Latency"
    assert data["days"] == 14
    # d1, d2 inside the window; d3 confirm is 20d ago (outside).
    ids = [rec["decision_id"] for rec in data["records"]]
    assert "d1" in ids
    assert "d2" in ids
    assert "d3" not in ids
    # d4 is OtherAlert — must be excluded
    assert "d4" not in ids


def test_list_feedback_filters_by_service(client):
    """service=kong drops d2 (spring-boot)."""
    r = client.get(
        "/tools/list_feedback",
        params={"alert_name": "HighP95Latency", "service": "kong"},
    )
    data = r.json()
    ids = [rec["decision_id"] for rec in data["records"]]
    assert ids == ["d1"]


def test_list_feedback_ordered_newest_first(client):
    """Records ordered by feedback.created_at DESC — d2 (1h) before d1 (2h)."""
    r = client.get(
        "/tools/list_feedback",
        params={"alert_name": "HighP95Latency"},
    )
    data = r.json()
    ids = [rec["decision_id"] for rec in data["records"]]
    assert ids == ["d2", "d1"]


def test_list_feedback_days_filter_widens_window(client):
    """days=30 brings in d3 which was outside the default 14d window."""
    r = client.get(
        "/tools/list_feedback",
        params={"alert_name": "HighP95Latency", "days": 30},
    )
    data = r.json()
    ids = [rec["decision_id"] for rec in data["records"]]
    assert "d3" in ids


def test_list_feedback_response_shape(client):
    """Response carries the documented fields per row."""
    r = client.get(
        "/tools/list_feedback",
        params={"alert_name": "HighP95Latency", "service": "spring-boot"},
    )
    data = r.json()
    assert data["count"] == 1
    rec = data["records"][0]
    # Expected keys
    for k in (
        "decision_id", "when", "feedback_type", "rating",
        "verdict_was_right", "action_was_right", "actual_cause",
        "tags", "notes", "alert_name", "service",
    ):
        assert k in rec, f"missing key {k!r} in response row"
    assert rec["decision_id"] == "d2"
    assert rec["feedback_type"] == "rate"
    assert rec["actual_cause"] == "JVM heap leak, restart only masked it"
    assert rec["alert_name"] == "HighP95Latency"
    assert rec["service"] == "spring-boot"


def test_list_feedback_returns_all_feedback_types(client):
    """Include rate AND confirm AND override rows (no feedback_type filter)."""
    # Widen window to 30 days to include the confirm row d3.
    r = client.get(
        "/tools/list_feedback",
        params={"alert_name": "HighP95Latency", "days": 30},
    )
    data = r.json()
    types = sorted({rec["feedback_type"] for rec in data["records"]})
    assert "rate" in types
    assert "confirm" in types


def test_list_feedback_respects_limit(client):
    r = client.get(
        "/tools/list_feedback",
        params={"alert_name": "HighP95Latency", "days": 30, "limit": 1},
    )
    data = r.json()
    assert len(data["records"]) == 1
