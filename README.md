# local-ai-suite

A local knowledge + tools layer for self-hosted LLMs. It exposes a single
**MCP gateway** that your clients (pi / pi.dev, OpenWebUI, or anything llama-server
drives) can call to search an offline knowledge base, the live web, PubMed, and
arXiv, and to do exact math.

The gateway is **passive**: it advertises tools and runs them. The model decides
which tool to call; the client harness runs the agent loop. See the full design
in the plan document (`docs/DESIGN.md`), including the phased roadmap and architecture
diagrams.

**Phase 1** is the offline knowledge base (Kiwix full-text) + online API tools.
**Phase 2** (in this build) adds the vector tier: `kb_search` becomes *hybrid*
(Kiwix lexical + Qdrant semantic → reranked), plus an incremental ingest pipeline
that embeds your own repos/notes. The vector tier is optional — with `QDRANT_URL`
or `EMBED_URL` unset, `kb_search` degrades to Kiwix-only, so Phase 1 still stands
on its own.

---

## Tools

| Tool | Backend | Notes |
|------|---------|-------|
| `kb_search` | Kiwix FTS (+ Qdrant vectors, reranked) | Offline; hybrid when the vector tier is on |
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
   <https://download.kiwix.org/zim/> and grab one. A small Wikipedia `nopic`
   build is a good first test (prefer `nopic`/`maxi` over `mini` — `mini` builds
   may lack the full-text index that `kb_search` needs):
   ```powershell
   ./scripts/download_zim.ps1 -Url "https://download.kiwix.org/zim/wikipedia/<pick-one>.zim"
   ```
   **Coding corpus (quick win):** for technical Q&A, also grab StackOverflow and
   DevDocs ZIMs — they're lexically searchable through `kb_search` immediately, no
   vector tier required:
   ```powershell
   # StackOverflow (~75 GB; use a _nopic build to save space) and DevDocs docsets
   ./scripts/download_zim.ps1 -Url "https://download.kiwix.org/zim/stack_exchange/<stackoverflow_...>.zim"
   ./scripts/download_zim.ps1 -Url "https://download.kiwix.org/zim/devdocs/<devdocs_en_python_...>.zim"
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

## Phase 2 — semantic search over your own code/notes

This adds the vector tier so `kb_search` also retrieves *your* corpora by meaning,
not just keywords. Four steps:

1. **Serve embeddings + a reranker** with llama-server (small, always-on),
   matching the endpoints in `config/.env`:
   ```powershell
   # embeddings (bge-m3) on :8081, reranker (bge-reranker-v2-m3) on :8082
   llama-server -m bge-m3.gguf --embedding --port 8081
   llama-server -m bge-reranker-v2-m3.gguf --reranking --port 8082
   ```
2. **Start Qdrant** (already in the compose file):
   ```powershell
   docker compose --env-file config/.env up -d qdrant
   ```
3. **Point `ingest/sources.yaml` at your repos/notes** (bounded — your code, not
   giant public corpora), then index. Only changed files are re-embedded on
   re-runs:
   ```powershell
   ./.venv/Scripts/python.exe -m ingest.ingest
   ```
4. Now `kb_search` merges Kiwix lexical hits with Qdrant semantic hits and reranks
   them. To keep it fresh, schedule the ingest command (Task Scheduler) to re-run
   on a cadence.

Verify: `python scripts/smoke_test.py` still passes, and after ingest a query
about your own code returns a `[curated]` result citing `label/path:line`.

## Configuration reference

All settings live in `config/.env` (see `config/.env.example` for the annotated
template): `DATA_ROOT`/`ZIM_DIR`, `KIWIX_URL`/`KIWIX_BOOK`, `KAGI_API_KEY`,
`NCBI_API_KEY`/`NCBI_EMAIL`, result limits, and the Phase 2 vector-tier settings
(`QDRANT_*`, `EMBED_*`, `RERANK_*`, `CHUNK_*`, `STATE_DB`).

## Development workflow

`main` is protected — no direct commits. All changes go through a pull request.

One-time setup to enable the local guard (blocks accidental pushes to `main`):

```powershell
git config core.hooksPath .githooks
```

(GitHub rejects branch-protection rulesets on this free-plan private repo —
the API returns "Upgrade to GitHub Pro or make this repository public to
enable this feature" — so the `.githooks/pre-push` hook enforces the same
rule locally in the meantime.)

Then, for each change:

```powershell
git checkout -b feat/short-description
# ... make changes ...
./.venv/Scripts/python.exe -m ruff check .   # lint
./.venv/Scripts/python.exe -m pytest         # tests
git add -A
git commit -m "..."
git push -u origin HEAD
gh pr create --fill
```

CI (`.github/workflows/ci.yml`) runs `ruff check` + `pytest` on every PR and must
pass before merging. Add or update tests under `tests/` alongside code changes.

## Roadmap

- **Phase 2 (done)** — Qdrant + `bge-m3` embeddings + `bge-reranker-v2-m3`;
  `kb_search` is hybrid (Kiwix FTS + vectors → rerank); incremental ingest of your
  own repos/notes/docs. StackOverflow/DevDocs ZIMs slot into Kiwix as above.
- **Phase 3 (next)** — SearXNG, `wolfram`/`units`/`datetime`, geospatial `route`
  tool, and retrieval-and-verify skills.

See `docs/DESIGN.md` for the full blueprint.
