FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shared/ shared/
COPY drain3_mcp/ drain3_mcp/
EXPOSE 8094
CMD ["uvicorn", "drain3_mcp.main:app", "--host", "0.0.0.0", "--port", "8094"]
