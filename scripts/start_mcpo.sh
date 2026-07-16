#!/bin/sh
set -eu
exec mcpo \
  --host 0.0.0.0 \
  --port 8000 \
  --api-key "$(cat /run/secrets/mcpo_api_key)" \
  --server-type streamable-http \
  --header "{\"Authorization\":\"Bearer $(cat /run/secrets/mcp_api_key)\"}" \
  -- http://las-gateway:8090/mcp
