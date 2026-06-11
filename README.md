# monitoring-mcp-servers

Six MCP (Model Context Protocol) bridge servers that give the LLM read-only access to the observability stack during AI-powered root cause analysis.

## Servers

| Server | Port | Upstream | Tools |
|--------|------|----------|-------|
| **Prometheus MCP** | 8091 | Prometheus API (:9090) | `query_instant`, `query_range`, `get_alerts`, `get_targets` |
| **Loki MCP** | 8092 | Loki API (:3100) | `query_logs`, `get_label_values`, `get_log_volume` |
| **Jaeger MCP** | 8093 | Jaeger API (:16686) | `find_traces`, `get_trace`, `get_services`, `get_operations` |
| **Drain3 MCP** | 8094 | Triage Service (:8090) | `get_clusters`, `get_anomaly_rate`, `match_log`, `get_baseline_info` |
| **RCA History MCP** | 8095 | SQLite DB (volume) | `get_recent_rcas`, `search_rcas`, `get_rca_detail`, `get_alert_frequency` |
| **Deploy MCP** | 8096 | Prometheus API (:9090) | `recent_deploys`, `last_deploy` |

The Deploy MCP derives deploy/rollout events from kube-state-metrics series already in Prometheus (`kube_replicaset_created` + `kube_replicaset_owner`) — no extra cluster access. It makes "a recent deploy caused this" a groundable claim, and "no deploys in the window" a grounded negative.

All servers are read-only FastAPI apps. Each has a `/health` endpoint and exposes tools under `/tools/`.

## Building

```bash
# Each server has its own Dockerfile
docker build -t cires/mcp-prometheus -f prometheus_mcp/Dockerfile .
docker build -t cires/mcp-loki -f loki_mcp/Dockerfile .
docker build -t cires/mcp-jaeger -f jaeger_mcp/Dockerfile .
docker build -t cires/mcp-drain3 -f drain3_mcp/Dockerfile .
docker build -t cires/mcp-rca-history -f rca_history_mcp/Dockerfile .
docker build -t cires/mcp-deploy -f deploy_mcp/Dockerfile .
```

## Related

- [monitoring-triage-service](https://github.com/linalaaraich/monitoring-triage-service) — The triage service that orchestrates LLM calls using these MCP bridges
- [monitoring-project](https://github.com/linalaaraich/monitoring-project) — Ansible playbooks deploying all services
