# Local AI Knowledge + Tools Suite — Design Blueprint

## Context

The goal is to extend the usefulness of capable-but-lower-knowledge local models
(e.g. Qwen3 ~27–30B served via llama-server) so they approach frontier-model
factual reliability through **retrieval + tools**, not bigger weights. Today the
stack is: llama-server (GGUF inference), **pi** (pi.dev, an MCP-native agent
harness), and **OpenWebUI** (already has Kagi web search). Hardware: single 32GB
NVIDIA/CUDA card.

Decisions captured from the design discussion:
- **Hybrid offline/online**: live web + APIs are primary; a *local* knowledge
  base is the appealing "no external dependency" fallback / second-opinion /
  verification layer. Build the offline tier, but don't mirror freshness-
  sensitive giants.
- **Domain priority**: coding/technical first; general, medical, and academic
  roughly equal after that.
- **Single unified MCP gateway** shared by all clients.

Guiding principle (the thing that makes this work): **tiered retrieval, not
pre-vectorizing the world.** Cheap full-text first stage (Kiwix/ZIM) → embed +
**rerank** only the candidates → reserve the vector DB for bounded, high-value
corpora. For small models, **precision > recall** (tight chunks + a reranker
matter more than corpus size).

---

## Target architecture

```
                 ┌────────────── Clients ──────────────┐
                 │  pi (MCP)   OpenWebUI (mcpo→OpenAPI) │
                 └───────────────────┬──────────────────┘
                                     │  tool calls
                          ┌──────────▼───────────┐
                          │   MCP Gateway        │  ← single server, the hub
                          │  kb_search           │
                          │  web_search          │
                          │  pubmed_search       │
                          │  arxiv_search        │
                          │  calculate / python  │
                          │  (wolfram, units…)   │
                          └───┬───────────┬──────┘
              ┌───────────────┘           └───────────────┐
       ┌──────▼──────┐  ┌────────────┐  ┌────────────┐  ┌─▼──────────┐
       │ Kiwix-serve │  │  Qdrant    │  │  SearXNG   │  │ Live APIs  │
       │ ZIM full-   │  │ curated    │  │ metasearch │  │ NCBI/arXiv │
       │ text (FTS)  │  │ vectors    │  │ (local)    │  │ Kagi/Wolf. │
       └─────────────┘  └────────────┘  └────────────┘  └────────────┘
                          ▲
                 ┌────────┴─────────┐
                 │ Ingestion script │  ← scheduled, incremental
                 └──────────────────┘

  Inference (existing + new small always-on models):
    llama-server: chat (Qwen3 ~30B, swappable) │ bge-m3 embed │ bge-reranker-v2-m3
```

### Layers

**1. Inference layer** (extend what exists)
- Keep llama-server for the chat model (Qwen3-VL-30B-A3B / Qwen3 27B).
- Add two *small, always-on* endpoints (they fit easily alongside the chat model
  — ~2GB each, GPU or even CPU):
  - **Embeddings**: `bge-m3` (recommended — dense+sparse+multivector hybrid, 8K
    ctx) or `Qwen3-Embedding-4B` (matches your Qwen ecosystem). Served via a
    llama-server instance with `--embedding`.
  - **Reranker**: `bge-reranker-v2-m3` via llama-server `--reranking`
    (`--pooling rank`). This is the single highest-leverage quality component.
- Optional: `llama-swap` to hot-swap only the *large* chat model on the 32GB
  card while embed/rerank stay resident.

**2. Retrieval layer**
- **Kiwix-serve** (Docker) hosting ZIM files — fully offline, *self-indexed
  full-text search* via its REST/OpenSearch API. No embedding needed. This is the
  bulk of the offline KB. (Decided: run kiwix-serve **directly** rather than via the
  Project NOMAD appliance — NOMAD wraps the same Kiwix/ZIMs but adds a black-box
  dependency plus unused extras (Kolibri/OSM/Ollama); direct serving keeps full
  programmatic control for the gateway.) Coding-first ZIM picks:
  - StackOverflow + key StackExchange sites (huge value for a coding model)
  - DevDocs (language/library API docs)
  - Wikipedia (`nopic` to save space) + Wiktionary
  - Wikipedia-medical subset (~2GB) for the medical slice
  - Project Gutenberg / Wikibooks (optional)
- **Qdrant** (Docker) — vector DB reserved for *bounded, high-value, semantic*
  corpora: your own repos/notes, selected PDFs/docs, curated medical set. NOT the
  whole of Wikipedia/PubMed.
- **SearXNG** (Docker) — local metasearch for web (no API key, local-first).
  Keep Kagi available too; expose both behind one `web_search` tool.

**3. MCP gateway** (the new core deliverable — one Python MCP server)
- Control flow: the gateway is **passive** — it advertises tool schemas and
  executes calls, but makes no decisions. The **model** chooses which tool to call;
  the **client harness** (pi / OpenWebUI) runs the agent loop, sends the model the
  prompt + tool list, relays the model's tool calls to the gateway (as MCP client),
  feeds results back, and repeats until the model writes a cited answer. The model
  never contacts the gateway directly — the harness mediates every hop.
- Tools exposed:
  - `kb_search(query, k)` — **hybrid local retrieval**: query Kiwix FTS *and*
    Qdrant in parallel → merge → **rerank** with bge-reranker → return top-k
    passages **with source + URL + date** (provenance for citation).
  - `web_search(query)` — SearXNG/Kagi.
  - `pubmed_search(query)` — NCBI E-utilities (live, always current).
  - `arxiv_search(query)` — arXiv API (live).
  - `calculate(expr)` / `python_exec(code)` — sandboxed Python (sympy/numpy) for
    math; more flexible than Wolfram for most needs.
  - Optional: `wolfram(query)` (API key), `units`, `datetime`.
  - Optional `route(origin, destination, mode)` — see geospatial subsystem below.
- **kb_search and web_search are deliberately separate** so the model (or a skill)
  can cross-check one against the other — your "second opinion / verify" goal.
- Distribution:
  - **pi** → point its MCP config at the gateway (native).
  - **OpenWebUI** → run `mcpo` to expose the gateway as an OpenAPI tool server
    (OpenWebUI's supported path); register that URL in OpenWebUI Tools.

**4. Ingestion / update pipeline** (the script you asked about)
- Orchestrated by a Python entrypoint, scheduled via **Windows Task Scheduler**.
- Three job types, each **incremental and idempotent**:
  - **ZIM refresh**: check `library.kiwix.org` for newer ZIMs by date/hash;
    download + atomically swap. Self-indexed → no embedding step.
  - **Curated re-index** (repos/notes/docs → Qdrant): walk sources → structure-
    aware chunk (markdown/code-aware; chunk code by symbol) → embed → upsert.
    Re-embed **only changed files** via a content-hash manifest (SQLite). This is
    what makes "updates regularly" cheap.
  - **API sources**: nothing to mirror (live) — config only.
- A `sources.yaml` declares every source + cadence; a `state.db` manifest tracks
  source/version/date/hash for both incrementality and provenance.

---

## Source catalog & incremental update strategy

Mental model: **full corpus on day 1, then pull only diffs.** The mechanism is
source-specific, in three tiers. Each source records a **watermark** (sequence #,
last-modified date, or content hash) in `state.db`; every run asks "what changed
since my watermark?" The only *expensive* step is embedding, which happens **only**
for curated→Qdrant sources — the big offline ZIM corpora are never embedded, so even
a "full re-pull" is just bandwidth.

| Source | Day-1 baseline | Ongoing delta | Watermark | Lives in |
|---|---|---|---|---|
| **PubMed/MEDLINE** | Annual baseline XML | **Daily updatefiles** (new/revised/deleted) — first-class diffs | file sequence # | live API primary; optional local abstract index |
| **arXiv** | Full metadata snapshot (Kaggle/S3) | OAI-PMH date-windowed harvest (`from`/`until`) | last harvest date | live API + curated vectors |
| **StackExchange/Overflow** | Quarterly dump → ZIM | SE API `sort=modified&since=` for fresh Q&A | last-modified ts | Kiwix ZIM (full re-pull) + API delta |
| **Wikipedia / Wiktionary** | Monthly ZIM (Kiwix) | New ZIM monthly (full re-pull); EventStreams SSE if live needed | ZIM build date | Kiwix ZIM |
| **DevDocs** | Full ZIM / scrape | Re-scrape on version bump | version tag | Kiwix ZIM |
| **NOMAD** *(only if materials science)* | API query all | API query by entry timestamp | last entry ts | **live API tool**, not embedded |
| **Your repos / notes / docs** | Initial walk + embed | git/mtime + **content hash** diff | content hash | Qdrant |

Three delta tiers:
- **First-class deltas** (PubMed) — apply the publisher's diff files directly.
- **Synthetic deltas** (arXiv, StackExchange, NOMAD) — date/timestamp-windowed API
  queries; you derive the diff.
- **Full re-pull** (Kiwix ZIMs) — no diff exists; replace the file. Cheap (download
  only, no re-embed).

NOMAD note: structured computational-materials data behind an API — include as a
live `nomad_search` gateway tool **only if** materials/comp-chem is a domain you
want; it is not a general-knowledge source and is never mirrored/embedded.

---

## Optional: geospatial / routing subsystem

To answer questions like "how long to drive from Cleveland to Cincinnati," map
data alone is insufficient — raw OSM is just the road network. Three pieces:
- **Routing engine** — Valhalla or OSRM builds a routable graph from OSM data and
  computes distance + duration.
- **Geocoder** — turn place names into coordinates. For city-level queries a tiny
  **GeoNames** gazetteer (tens of MB) suffices; avoid full Nominatim (~TB at planet
  scale).
- **Gateway tool** — `route(origin, destination, mode)` → distance + ETA.

This is a separate geospatial subsystem, not part of RAG. Fully local/offline.
Scope OSM to a region (a few US states) to keep it small, or North America for
broader coverage. Defer until the core KB is working.

---

## Storage budget

PubMed and arXiv are live APIs (~0 storage). Disk cost is ZIM corpora + vectors +
models (+ optional maps). Approximate:

| Component | Size |
|---|---|
| Wikipedia EN, no images (ZIM) | ~55 GB |
| Wikipedia EN, with images | ~100 GB |
| StackOverflow (ZIM) | ~75 GB |
| Other StackExchange (curated) | ~10–20 GB |
| DevDocs / Wiktionary / Wikimed | ~8 GB combined |
| Qdrant vectors (your repos/notes) | ~1–10 GB |
| Models (chat Q4 + embed + reranker) | ~20 GB |
| Maps — OSM NA extract + Valhalla tiles (optional) | ~45 GB (or ~5 GB regional) |

**Lean coding-first build** ≈ 150 GB; **full build** (Wikipedia w/ images + NA
routing) ≈ 300 GB. Provision a dedicated **500 GB–1 TB** drive for headroom.

### Data lives outside the repo, on paths you choose
- **Setup never auto-downloads corpora.** `docker compose up` + installing the
  gateway scaffolds infra only. Pulling ZIMs, importing to Qdrant, and building
  routing tiles are separate, explicit `make`/script targets you run deliberately.
- **All data paths are configurable** via `config/.env` — a single `DATA_ROOT`
  (e.g. `D:\ai-data`) with `ZIM_DIR`, `QDRANT_STORAGE`, `MODEL_DIR`, `STATE_DB`
  under it. Docker services **bind-mount** those host paths, so the large data sits
  on the drive you pick, wholly separate from the repo. The `zim/` and `state.db`
  entries in the layout below are defaults that point at `DATA_ROOT`, not fixed
  in-repo locations.

---

## Proposed repo layout

New repo `local-ai-suite/` under `C:\Users\Tom\Documents\Repos`:

```
local-ai-suite/
  docker-compose.yml          # qdrant, kiwix-serve, searxng, mcpo
  config/.env                 # API keys, endpoints (gitignored)
  mcp_gateway/
    server.py                 # FastMCP server, registers tools
    tools/{kb_search,web_search,pubmed,arxiv,compute}.py
  retrieval/
    hybrid.py                 # kiwix FTS + qdrant query + rerank merge
    embed.py  rerank.py       # thin clients to llama-server endpoints
  ingest/
    ingest.py                 # incremental indexer (the maintenance script)
    sources.yaml              # declarative source list + cadence
    state.db                  # sqlite manifest (gitignored)
  zim/                        # ZIM files (gitignored — large)
  scripts/
    refresh.ps1               # entrypoint the scheduled task runs
    register_task.ps1         # one-time Windows Task Scheduler registration
  README.md
```

Stack choices: Python + FastMCP (MCP server), `qdrant-client`, `httpx`
(Kiwix/APIs), and the llama-server HTTP API for embed/rerank. Keep dependencies
lean — avoid pulling in a heavyweight framework (LlamaIndex/Haystack) unless a
need emerges; the hybrid logic is ~one module.

---

## Build order (phased)

### Phase 1 — MVP: local Wikipedia + online APIs (no vector stack)
Local Wikipedia is Kiwix full-text search, which needs **no embeddings**, so this
phase skips Qdrant/embed/rerank entirely and still delivers a working, useful hub.
1. Repo skeleton + `docker-compose` for **kiwix-serve only**; `config/.env` with
   `DATA_ROOT`/`ZIM_DIR`. Download **one** ZIM (Wikipedia-nopic) to prove FTS.
2. MCP gateway (`server.py`) exposing `kb_search` (Kiwix FTS), `web_search`,
   `pubmed_search`, `arxiv_search`, `calculate`. Test standalone with an inspector.
3. Client wiring: connect **pi** (native MCP); run **mcpo** and register the
   OpenAPI URL in **OpenWebUI**. Confirm both can call the tools + get cited answers.

### Phase 2 — semantic search over your own data
4. Inference endpoints: bge-m3 embed + bge-reranker-v2-m3 on llama-server; smoke-test.
5. Add **Qdrant** to compose; `retrieval/hybrid.py` upgrades `kb_search` to hybrid
   (Kiwix FTS + Qdrant → rerank → top-k with provenance).
6. `ingest.py` incremental indexer + `sources.yaml` (watermark/hash diff) for your
   repos/notes/docs; register `refresh.ps1` with Task Scheduler.
7. Add coding ZIMs (StackOverflow, DevDocs) to the Kiwix library.

### Phase 3 — extras (opt-in, independent)
8. SearXNG (if replacing/augmenting Kagi); `wolfram`, `units`, `datetime` tools.
9. Geospatial/routing subsystem + `route` tool (Valhalla/OSRM + GeoNames).
10. Skills/prompts: retrieval-and-verify pattern (cite sources; cross-check web
    claims against the local KB — your "second opinion").

---

## Verification (end-to-end)

**Phase 1 acceptance gate** (do this before building the vector stack):
- `docker compose up` → Kiwix (`:8080`) reachable; `curl` its search API for a
  known term → returns hits (FTS works).
- Call `kb_search`, `web_search`, `pubmed_search`, `arxiv_search`, `calculate` on
  the gateway directly → sensible results.
- In **pi** and **OpenWebUI** (via mcpo): ask a Wikipedia-answerable question → the
  model calls `kb_search` and answers with a citation; ask a math question →
  `calculate` fires. If this holds, Phase 1 is done.

**Phase 2+ checks:**
- `docker compose up` also brings Qdrant (`:6333`); SearXNG later.
- Embed + rerank smoke test: POST a query/doc pair to the llama-server reranker,
  confirm a sensible score ordering.
- `python -m ingest.ingest` on a small repo → vectors appear in Qdrant; re-run
  with no changes → **zero re-embeds** (incrementality holds).
- Call `kb_search("…")` directly on the MCP gateway → top-k passages **with
  source/URL/date**.
- In **pi**: ask a question that needs retrieval (e.g. an obscure library API) →
  model invokes `kb_search`, answers **with citation**. Repeat in **OpenWebUI**
  via the mcpo tool. Ask a math question → `calculate`/`python_exec` fires.
- Cross-check test: a question where web and KB might disagree → confirm the
  model can call both and reconcile.

## Open considerations (decide during build)
- Exact ZIM set + total disk budget for `zim/` (StackOverflow alone is tens of GB).
- Whether to add `llama-swap` now or only once GPU contention appears.
- Sandboxing approach for `python_exec` (subprocess + resource limits vs. a
  container) — start restrictive.
