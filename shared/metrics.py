"""Prometheus /metrics exposure for every MCP server.

Uses prometheus-fastapi-instrumentator to auto-instrument FastAPI routes
and expose the default Prometheus metrics at /metrics. Each MCP server
calls install_metrics(app) once after constructing its FastAPI app.
"""
from prometheus_fastapi_instrumentator import Instrumentator


def install_metrics(app):
    """Install /metrics on the given FastAPI app.

    Metrics exposed (via prometheus-fastapi-instrumentator defaults):
      - http_requests_total{method, handler, status}
      - http_request_duration_seconds{method, handler}
      - http_request_size_bytes / http_response_size_bytes
      - process_* (resident memory, CPU time, start time)
      - python_gc_*
    """
    Instrumentator().instrument(app).expose(app, include_in_schema=False)
