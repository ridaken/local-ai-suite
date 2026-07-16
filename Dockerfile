# Shared image for the split MCP gateway and admin service. Compose selects the
# entrypoint and grants each process only the mounts it needs.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mcp_gateway/ mcp_gateway/
COPY retrieval/ retrieval/
COPY ingest/ ingest/

ENV LAS_TRANSPORT=http
EXPOSE 8090 8091

CMD ["python", "-m", "mcp_gateway.server"]
