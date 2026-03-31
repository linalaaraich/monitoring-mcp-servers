#!/usr/bin/env bash
# Build all MCP server Docker images
set -euo pipefail

echo "Building MCP server images..."

docker build -t cires/mcp-prometheus -f prometheus_mcp/Dockerfile .
echo "  cires/mcp-prometheus built"

docker build -t cires/mcp-loki -f loki_mcp/Dockerfile .
echo "  cires/mcp-loki built"

docker build -t cires/mcp-jaeger -f jaeger_mcp/Dockerfile .
echo "  cires/mcp-jaeger built"

docker build -t cires/mcp-drain3 -f drain3_mcp/Dockerfile .
echo "  cires/mcp-drain3 built"

docker build -t cires/mcp-rca-history -f rca_history_mcp/Dockerfile .
echo "  cires/mcp-rca-history built"

echo ""
echo "All 5 MCP images built:"
docker images --filter "reference=cires/mcp-*" --format "  {{.Repository}}:{{.Tag}}  {{.Size}}"
