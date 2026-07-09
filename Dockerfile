# Gateway service: MCP-over-HTTP (/mcp) + the admin UI (/), in one small
# image. kiwix-serve and qdrant stay upstream images (see docker-compose.yml)
# so their own upgrade cadence isn't tangled up with ours.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mcp_gateway/ mcp_gateway/
COPY retrieval/ retrieval/
COPY ingest/ ingest/

ENV LAS_TRANSPORT=http
EXPOSE 8090

CMD ["python", "-m", "mcp_gateway.server"]
