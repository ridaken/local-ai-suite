# local-ai-suite

local-ai-suite gives your local LLM a practical set of research tools: an
offline knowledge base, live scholarly and web search, and exact calculation,
all exposed through one MCP gateway.

It is designed for a Windows or Docker Desktop workstation running local AI
clients such as pi, OpenWebUI, or any MCP-capable harness. Start the stack, open
the admin page, download the reference libraries you want, and point your client
at the gateway.

## What You Get

- Offline knowledge search over Kiwix ZIM libraries, including Wikipedia,
  Stack Overflow, DevDocs, and other catalog content.
- Optional semantic search over your own repos, notes, and documents through
  Qdrant plus a local embedding model.
- A browser admin UI for checking service health, browsing and downloading ZIMs,
  enabling or disabling sources, and choosing retrieval mode.
- MCP tools for knowledge-base search, live web search, PubMed, arXiv, and safe
  math.
- Two ways to connect: stdio for desktop MCP clients, or HTTP at `/mcp` from the
  containerized gateway.

## Tools

| Tool | Use it for | Works offline? |
| --- | --- | --- |
| `kb_search` | Local reference answers from installed ZIMs and, if enabled, your indexed files | Yes |
| `kb_read` | Full article text behind a `kb_search` result, a few thousand characters per page | Yes |
| `calculate` | Exact arithmetic and common math functions | Yes |
| `web_search` | Current web results through Kagi | No, needs `KAGI_API_KEY` |
| `pubmed_search` | Biomedical literature citations from PubMed | No |
| `arxiv_search` | Preprint search from arXiv | No |

## Quick Start

### 1. Start The Stack

You need Docker Desktop. From the repo root:

```powershell
docker compose up -d --build
```

Then open:

- Admin UI: <http://localhost:8090>
- Kiwix library: <http://localhost:8080>

With no configuration file, the stack uses `./data/zim` in this repo for ZIM
files and `./data/qdrant` for vectors. That is fine for a first run. For
long-term use, point storage at a real data drive.

### 2. Pick A Data Folder

Copy the example config and edit the paths:

```powershell
Copy-Item config/.env.example config/.env
```

Set at least:

```dotenv
DATA_ROOT=D:/ai-data
ZIM_DIR=D:/ai-data/zim
QDRANT_STORAGE=D:/ai-data/qdrant
STATE_DB=D:/ai-data/state.db
SETTINGS_DB=D:/ai-data/settings.db
```

Restart after changing config:

```powershell
docker compose --env-file config/.env up -d --build
```

### 3. Add Offline Knowledge

Open <http://localhost:8090> and use:

- **Recommended** for starter downloads such as English Wikipedia, Stack
  Overflow, Python docs, and web docs.
- **Catalog** to search the public Kiwix catalog and download any ZIM into your
  library.
- **Sources** to see installed books, disable books from search, or delete them.
- **Downloads** to track active downloads.
- **Configuration** to set API keys, service URLs, default limits, model
  endpoints, and storage-related paths.

Kiwix hot-reloads the generated `library.xml`, so completed downloads become
available without restarting the stack.

### 4. Connect An AI Client

For an MCP client that can talk to streamable HTTP, use:

```text
http://localhost:8090/mcp
```

For clients that launch an MCP server over stdio, install the Python
dependencies and point the client at `mcp_gateway.server`:

```powershell
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
```

Example stdio server config:

```json
{
  "mcpServers": {
    "local-ai-suite": {
      "command": "C:/Users/Tom/Documents/Repos/local-ai-suite/.venv/Scripts/python.exe",
      "args": ["-m", "mcp_gateway.server"],
      "cwd": "C:/Users/Tom/Documents/Repos/local-ai-suite"
    }
  }
}
```

### OpenWebUI

OpenWebUI consumes tools as an OpenAPI server, so it needs the `mcpo` bridge in
front of the gateway. The stack ships an `mcpo` service that does this for you —
it connects to the running gateway's `/mcp` endpoint (so it honors the admin
UI's per-book toggles) and exposes every tool as an OpenAPI path.

1. Set `MCPO_API_KEY` in `config/.env` to a long random string (the bridge is
   published on the host, so this key is what keeps your LAN from calling the
   tools). `docker compose up -d` starts `las-mcpo` on `MCPO_PORT` (default
   `8000`).
2. In OpenWebUI, go to **Settings → Tools → Add Connection** and add:
   - URL: `http://host.docker.internal:8000` (OpenWebUI runs in a container, so
     `localhost` there is not your host — use `host.docker.internal`; if
     OpenWebUI runs directly on the host, `http://localhost:8000` is fine).
   - API key: the `MCPO_API_KEY` value.
3. OpenWebUI must have a **chat model that supports tool calling** configured
   (e.g. your llama-server as an OpenAI-compatible connection). Tool calling is
   driven by the model, so without one the tools will be registered but never
   invoked.

Ask a Wikipedia-answerable question and the model should call `kb_search` (then
`kb_read` for detail) and answer with a citation.

For the system prompt and settings that make the model actually *use* retrieval
well (verify-and-cite behaviour, Native function calling, tool scoping), plus the
OpenWebUI-vs-pi split, see [docs/prompts.md](docs/prompts.md).

> To run the bridge without Docker instead, install mcpo and point it at the
> gateway: `mcpo --port 8000 --server-type streamablehttp -- http://localhost:8090/mcp`.

## Optional: Live Web Search

`web_search` uses the Kagi Search API. Add your token to `config/.env`:

```dotenv
KAGI_API_KEY=your-token
```

If this is blank, the tool returns a clear "not configured" message and the
other tools continue to work.

## Optional: Semantic Search Over Your Files

The local knowledge search can combine Kiwix full-text results with semantic
matches from your own curated files.

1. Run Qdrant through Docker Compose. It is included in the default stack.
2. Serve an embedding model and reranker with OpenAI-compatible endpoints:

   ```powershell
   llama-server -m bge-m3.gguf --embedding --port 8081
   llama-server -m bge-reranker-v2-m3.gguf --reranking --port 8082
   ```

3. Edit `ingest/sources.yaml` to list the repos, notes, or document folders you
   want indexed.
4. Run the incremental ingest:

   ```powershell
   ./.venv/Scripts/python.exe -m ingest.ingest
   ```

In the admin UI, use **Settings** to choose:

- **Hybrid**: Kiwix full-text plus vectors.
- **Lexical only**: Kiwix full-text only.
- **Vector only**: your indexed files only.
- Reranking on or off.

If Qdrant, embeddings, or the reranker are unavailable, `kb_search` degrades to
the tiers that are still reachable and reports partial-result warnings.

## Running The Gateway Directly

The server defaults to stdio:

```powershell
./.venv/Scripts/python.exe -m mcp_gateway.server
```

To run the admin UI and HTTP MCP endpoint directly on the host instead of
through Docker:

```powershell
$env:LAS_TRANSPORT="http"
./.venv/Scripts/python.exe -m mcp_gateway.server
```

By default the admin service binds to `127.0.0.1:8090`. The admin UI has no
authentication, so expose it beyond localhost only on a trusted network.

## Configuration

Settings can be changed from the admin UI's **Configuration** page. Saved
values are stored in `SETTINGS_DB` and override environment/config-file values
when the hosted gateway starts; most API keys, URLs, limits, and model endpoints
also apply immediately when you save.

The gateway still reads environment variables and, if present, `config/.env` as
its initial defaults. Real environment variables win over the file before admin
overrides are applied.

Common settings:

| Setting | Purpose |
| --- | --- |
| `ZIM_DIR` | Folder where ZIM files and `library.xml` live |
| `KAGI_API_KEY` | Enables live web search |
| `KIWIX_URL` | URL the gateway uses for Kiwix |
| `QDRANT_URL`, `QDRANT_COLLECTION` | Vector database location and collection |
| `EMBED_URL`, `EMBED_MODEL`, `EMBED_DIM` | Embedding endpoint |
| `RERANK_URL`, `RERANK_MODEL` | Reranker endpoint |
| `STATE_DB` | Incremental ingest manifest |
| `SETTINGS_DB` | Runtime admin settings |
| `ADMIN_HOST`, `ADMIN_PORT` | Admin UI and HTTP MCP bind address |

Runtime choices made in the admin UI, such as retrieval mode, reranking, and
per-book enablement, are also stored in `SETTINGS_DB` and take effect
immediately.

Some values describe the running process or Docker bind mounts. Changes to
`ADMIN_HOST`, `ADMIN_PORT`, `SETTINGS_DB`, and compose-managed storage paths
such as host-side `ZIM_DIR` or `QDRANT_STORAGE` are saved in the UI, but they
need a gateway restart or Docker Compose recreation before the underlying bind
or listener changes.

## Smoke Test

After installing Python dependencies:

```powershell
./.venv/Scripts/python.exe scripts/smoke_test.py
```

A healthy first setup should show Kiwix results once at least one searchable ZIM
is installed. PubMed, arXiv, and calculate do not need local corpora. Web search
needs `KAGI_API_KEY`.

## For Maintainers

The project is intentionally split into small pieces:

- `mcp_gateway/` serves MCP tools, the admin UI, downloads, catalog search, and
  runtime settings.
- `retrieval/` merges Kiwix lexical search with optional Qdrant vector search
  and reranking.
- `ingest/` chunks and embeds curated local files into Qdrant.
- `tests/` covers gateway behavior, admin flows, retrieval, ingest, downloads,
  recommendations, and settings.

Useful checks:

```powershell
./.venv/Scripts/python.exe -m ruff check .
./.venv/Scripts/python.exe -m pytest
```

`main` is protected by the local pre-push hook. Enable it once with:

```powershell
git config core.hooksPath .githooks
```

See `docs/DESIGN.md` for deeper architecture notes and future roadmap ideas.
