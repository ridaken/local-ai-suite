# local-ai-suite

A local knowledge + tools layer for self-hosted LLMs. It exposes a single
**MCP gateway** that your clients (pi / pi.dev, OpenWebUI, or anything llama-server
drives) can call to search an offline knowledge base, the live web, PubMed, and
arXiv, and to do exact math.

The gateway is **passive**: it advertises tools and runs them. The model decides
which tool to call; the client harness runs the agent loop. See the full design
in the plan document (`docs/DESIGN.md`), including the phased roadmap and architecture
diagrams.

**This is Phase 1** — local Wikipedia (Kiwix full-text search) + online API tools,
with no vector database or embeddings yet. Semantic search over your own repos
(Qdrant + reranker) arrives in Phase 2.

---

## What you get in Phase 1

| Tool | Backend | Notes |
|------|---------|-------|
| `kb_search` | kiwix-serve full-text | Offline; needs a ZIM loaded |
| `web_search` | Kagi Search API | Needs `KAGI_API_KEY` |
| `pubmed_search` | NCBI E-utilities | Live; key optional (higher rate limit) |
| `arxiv_search` | arXiv API | Live |
| `calculate` | safe AST evaluator | Offline; no code execution |

---

## Prerequisites

- **Docker Desktop** (for kiwix-serve)
- **Python 3.11+**
- A data drive with room for ZIM files (see the storage budget in the plan)

## Setup

1. **Configure paths and keys.** Copy the template and edit it:
   ```powershell
   Copy-Item config/.env.example config/.env
   ```
   Set `ZIM_DIR` to a folder on your data drive (absolute path), and add
   `KAGI_API_KEY` if you want web search. Nothing here points into the repo — the
   large data lives wherever you choose.

2. **Get a ZIM (explicit — nothing auto-downloads).** Browse
   <https://download.kiwix.org/zim/> and grab one. A small Wikipedia `nopic`/`mini`
   build is a good first test:
   ```powershell
   ./scripts/download_zim.ps1 -Url "https://download.kiwix.org/zim/wikipedia/<pick-one>.zim"
   ```

3. **Start kiwix-serve:**
   ```powershell
   docker compose --env-file config/.env up -d
   ```
   Open <http://localhost:8080> — you should see your book. It serves every
   `*.zim` in `ZIM_DIR`; add more later and `docker compose ... restart kiwix`.

4. **Install the gateway (project-local venv):**
   ```powershell
   python -m venv .venv
   ./.venv/Scripts/python.exe -m pip install -r requirements.txt
   ```

5. **Smoke-test the tools:**
   ```powershell
   ./.venv/Scripts/python.exe scripts/smoke_test.py
   ```

## Run the gateway

The gateway speaks MCP over **stdio** by default, which works for both pi and the
mcpo bridge:
```powershell
./.venv/Scripts/python.exe -m mcp_gateway.server
```
(You normally don't run it by hand — the client launches it. See wiring below.)

---

## Wire it into your clients

### pi (pi.dev) — native MCP

pi speaks MCP directly. Add the gateway as an MCP **stdio** server in pi's MCP
config, using the standard shape:

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

pi will launch the server, discover the five tools, and let the model call them.

### OpenWebUI — via the mcpo bridge

OpenWebUI consumes **OpenAPI tool servers**, so run
[`mcpo`](https://github.com/open-webui/mcpo) to expose this MCP server as OpenAPI:

```powershell
# one-time: install mcpo (or use `uvx mcpo ...` without installing)
pip install mcpo

# serve the gateway as OpenAPI on :8000
mcpo --port 8000 -- C:/Users/Tom/Documents/Repos/local-ai-suite/.venv/Scripts/python.exe -m mcp_gateway.server
```

Then in OpenWebUI: **Settings → Tools → add a connection** pointing at
`http://localhost:8000` (docs at `http://localhost:8000/docs`). The five tools
appear and become callable by your models.

---

## Phase 1 acceptance gate

You're done with Phase 1 when all of these pass:

- `http://localhost:8080` shows your ZIM, and `kb_search` returns hits in the
  smoke test.
- `pubmed_search` / `arxiv_search` return citations; `calculate` returns exact
  answers; `web_search` returns results (with a Kagi key).
- In **pi**: ask a Wikipedia-answerable question → the model calls `kb_search` and
  answers with a source URL. Ask a math question → `calculate` fires.
- In **OpenWebUI** (via mcpo): the same tools are callable and cited.

## Configuration reference

All settings live in `config/.env` (see `config/.env.example` for the annotated
template): `DATA_ROOT`/`ZIM_DIR`, `KIWIX_URL`/`KIWIX_BOOK`, `KAGI_API_KEY`,
`NCBI_API_KEY`/`NCBI_EMAIL`, and default result limits.

## Development workflow

`main` is protected — no direct commits. All changes go through a pull request.

One-time setup to enable the local guard (blocks accidental pushes to `main`):

```powershell
git config core.hooksPath .githooks
```

(Server-side branch protection needs GitHub Pro on a private repo; the
`.githooks/pre-push` hook enforces the same rule locally in the meantime.)

Then, for each change:

```powershell
git checkout -b feat/short-description
# ... make changes ...
./.venv/Scripts/python.exe -m ruff check .   # lint
./.venv/Scripts/python.exe -m pytest         # tests
git commit -am "..."
git push -u origin HEAD
gh pr create --fill
```

CI (`.github/workflows/ci.yml`) runs `ruff check` + `pytest` on every PR and must
pass before merging. Add or update tests under `tests/` alongside code changes.

## Roadmap

- **Phase 2** — Qdrant + `bge-m3` embeddings + `bge-reranker-v2-m3`; `kb_search`
  becomes hybrid (Kiwix FTS + vectors → rerank); incremental ingest of your own
  repos/notes/docs; add StackOverflow/DevDocs ZIMs.
- **Phase 3** — SearXNG, `wolfram`/`units`/`datetime`, geospatial `route` tool,
  and retrieval-and-verify skills.

See `docs/DESIGN.md` for the full blueprint.
