# local-ai-suite

local-ai-suite is a local-first MCP server for offline Kiwix search, hybrid
Qdrant retrieval, web and research tools, and bounded utility tools. Version 0.2
splits the MCP data plane from the administrator control plane, fails closed
when hosted credentials or state are missing, and keeps download work durable
across restarts.

## v0.2 security boundary

The default Compose stack runs two application services:

- `las-gateway` on `127.0.0.1:8090` exposes `/mcp`, the versioned `/api/v1`
  JSON API, and `/healthz`/`/readyz`. MCP and API routes require
  `Authorization: Bearer <MCP_API_KEY>`.
- `las-admin` on `127.0.0.1:8091` exposes the management UI and download worker.
  It requires `ADMIN_TOKEN`, an authenticated session, and CSRF protection.

Kiwix (`127.0.0.1:8080`) and Qdrant (`127.0.0.1:6333`) are also loopback-only.
The gateway reads corpus and state mounts read-only. Only the admin service can
write corpus and state. Kiwix receives neither state nor secret mounts.

`python -m mcp_gateway.server` remains a credential-free stdio MCP server. The
credentials below are mandatory only for hosted HTTP services.

## Fresh installation

Requirements: Docker with Compose, PowerShell 5.1 or newer for the setup helper,
and enough disk space for the ZIM corpus you intend to install.

1. Copy `config/.env.example` to `config/.env` and set `ZIM_DIR`, `STATE_DIR`,
   and `QDRANT_STORAGE` to host paths you control. Keep them separate.
2. Run the setup launcher:

   ```powershell
   .\setup.cmd
   ```

   The launcher works even when PowerShell's script execution policy is restricted.
   It creates missing credentials, repairs empty directory placeholders left by a
   premature Docker start, validates the resolved Compose configuration, and starts
   the stack detached. It never prints credential values. Put a Kagi or NCBI API
   key into its matching file if you use that provider; empty files leave those
   integrations disabled.
3. For manual setup or troubleshooting, run the individual helpers:

   ```powershell
   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
   .\scripts\init-secrets.ps1
   .\scripts\compose.ps1 config --quiet
   .\scripts\compose.ps1 up -d --build
   ```

   The wrapper always loads `config/.env` for both Compose interpolation and
   service configuration, and prints resolved storage mounts and published ports
   before running the command. It works from any current directory. Without
   PowerShell, run from the repository root with
   `docker compose --env-file config/.env up -d --build`; use the same `--env-file`
   argument for every Compose command. Service `env_file` alone does not configure
   bind mounts or published ports.

   `config-validator` checks credential length, placeholders, duplicates, and
   unsafe MCP Host configuration before state initialization or either hosted
   application starts.
4. Open <http://localhost:8091>, paste the value from
   `config/secrets/admin_token.txt`, and sign in. Do not expose the admin port
   through a LAN bind or reverse proxy without also configuring HTTPS, trusted
   Hosts/Origins, and `ADMIN_COOKIE_SECURE=1`.

The admin configuration screen is deliberately read-only for infrastructure,
endpoints, and secrets. Retrieval mode, reranking, and per-book toggles remain
editable.

## MCP clients

For a host client, use:

- URL: `http://localhost:8090/mcp`
- Header: `Authorization: Bearer <contents of mcp_api_key.txt>`

For a client container attached to the named `las-clients` network, use the
native MCP connection:

- URL: `http://las-gateway:8090/mcp`
- Header: `Authorization: Bearer <MCP_API_KEY>`

Only the gateway and client integrations join `las-clients`; Qdrant, Kiwix, and
admin stay on the backend side of the boundary.

### OpenWebUI (native MCP)

OpenWebUI connects directly to the gateway over streamable HTTP. The suite is an
external **tool server**, not an OpenWebUI Knowledge collection or model-provider
connection.

Attach an existing OpenWebUI container to the client network (replace
`open-webui` if the container has a different name):

```powershell
docker network connect las-clients open-webui
```

A manual attachment survives container restarts but not container replacement.
For a Compose-managed OpenWebUI deployment, make the attachment durable in that
deployment's Compose file:

```yaml
services:
  open-webui:
    networks:
      - default
      - las-clients

networks:
  las-clients:
    external: true
```

In OpenWebUI, go to **Settings > Admin > Integrations > External Tool Servers**
and add:

- Type: `MCP (Streamable HTTP)` (not OpenAPI)
- Name: `Local AI Suite`
- ID: `local_ai_suite`
- URL: `http://las-gateway:8090/mcp`
- Auth: `Bearer`
- Key: the exact contents of `config/secrets/mcp_api_key.txt`

On Windows, copy the credential without displaying it or adding whitespace:

```powershell
(Get-Content -Raw .\config\secrets\mcp_api_key.txt).Trim() | Set-Clipboard
```

Save the server, grant the intended users access, and enable it for the relevant
model or chat. Common setup failures are diagnostic:

- **Ollama: Network Problem** means the MCP URL was added under the Ollama/model
  provider Connections page instead of Integrations.
- A request to `/mcp/openapi.json` means the server type is OpenAPI instead of
  MCP Streamable HTTP.
- `401 Unauthorized` means the bearer value is missing or does not exactly match
  `mcp_api_key.txt`; `mcpo_api_key.txt` is a different credential.
- A DNS or connection error for `las-gateway` means the OpenWebUI container is
  not attached to `las-clients`.

See the [OpenWebUI MCP documentation](https://docs.openwebui.com/features/extensibility/mcp/)
for client-version-specific UI details.

For research profiles, enable `pubmed_search`, `arxiv_search`, `article_find`,
and `article_read` together and paste the profile from `docs/prompts.md` into the
custom model. Search returns abstracts for choosing papers; the article tools
perform the auditable full-text/passages step. After rebuilding the gateway,
reconnect or refresh the OpenWebUI integration if its cached tool list does not
show the two article tools.

The tools themselves remain usable without the custom profile. Their descriptions
and search-result fallbacks identify PubMed/arXiv results as candidates and direct
the client to `article_find`/`article_read`. The profile makes that multi-call
behavior more consistent for smaller local models; it is not a private OpenWebUI
feature or a prerequisite for other MCP clients.

## JSON API clients

Scripts and applications that do not implement MCP can call the same operations
through authenticated JSON endpoints. Discover the routes and research workflow
at `/api/v1`; retrieve a machine-readable OpenAPI 3.1 document from
`/api/v1/openapi.json`. The JSON responses use the exact same Pydantic models as
MCP `structuredContent`, including `article_id`, `content_level`, citations,
offsets, warnings, and typed errors.

```powershell
$lasToken = (Get-Content -Raw config/secrets/mcp_api_key.txt).Trim()
$lasHeaders = @{ Authorization = "Bearer $lasToken" }

$papers = Invoke-RestMethod -Method Post `
  -Uri http://localhost:8090/api/v1/pubmed/search `
  -Headers $lasHeaders -ContentType application/json `
  -Body (@{ query = "sepsis treatment guidelines"; limit = 5 } | ConvertTo-Json)

$evidence = Invoke-RestMethod -Method Post `
  -Uri http://localhost:8090/api/v1/articles/find `
  -Headers $lasHeaders -ContentType application/json `
  -Body (@{
    article_id = $papers.results[0].article_id
    query = "antimicrobial timing and initial fluid resuscitation"
    limit = 5
  } | ConvertTo-Json)
```

Available endpoints cover all gateway tools:

- `/api/v1/kb/search` and `/api/v1/kb/read`
- `/api/v1/web/search`
- `/api/v1/pubmed/search` and `/api/v1/arxiv/search`
- `/api/v1/articles/find` and `/api/v1/articles/read`
- `/api/v1/calculate`

Search responses retain complete abstracts in JSON. Their prose fallback shown to
LLMs uses a bounded preview so an older, unusually long abstract cannot crowd out
candidate selection and full-text retrieval.

### Optional legacy mcpo bridge

Use `mcpo` only for an OpenWebUI version without native streamable-HTTP MCP
support. It is not started by default and publishes no host port. Attach
OpenWebUI to `las-clients` as described above, then enable the bridge:

```powershell
.\scripts\compose.ps1 -Legacy up -d mcpo
```

The bridge reads its client-facing `MCPO_API_KEY` and downstream `MCP_API_KEY`
from Docker secret files. It never receives them as command-line values from the
host or exposes port 8000 on the host. Register it in OpenWebUI as an **OpenAPI**
external tool server at `http://mcpo:8000`, using Bearer auth with the contents
of `config/secrets/mcpo_api_key.txt`. Do not point an old, separately created
MCPO container at the authenticated v0.2 gateway; recreate it through the wrapper
above so it receives the downstream gateway credential and correct network.

## Migrating a v0.1 installation

Version 0.1 stored runtime settings and provider secrets beside the ZIM corpus.
Version 0.2 refuses hosted readiness while that legacy `settings.db` remains.

Stop the old stack and configure the new `ZIM_DIR` and `STATE_DIR`. The setup
launcher detects the legacy database, prints a dry-run migration preview, and
stops with the command needed to approve it:

```powershell
.\setup.cmd
.\setup.cmd -ApplyMigration
```

To run the migration separately, use the helper below. It resolves the corpus
and state paths from Compose, so blank variables using Compose defaults and paths
containing spaces work consistently. The command is dry-run-only unless `-Apply`
is supplied:

```powershell
.\scripts\migrate_v02.ps1
```

Review every reported path, then apply:

```powershell
.\scripts\migrate_v02.ps1 -Apply
```

The migration:

- copies the legacy database to `STATE_DIR/settings.db.v01.bak` first;
- copies only retrieval behavior and book toggles into the new state database;
- moves Kagi and NCBI values to non-overwriting secret files;
- creates missing strong admin and MCP credentials without printing them; and
- removes the corpus-side database only after backup and target verification.

Existing non-empty secret files are never overwritten; empty placeholders may be
populated from the legacy database or replaced with generated service credentials.
If any apply step fails, the legacy database remains in place. Resolve the failure
before retrying; a present backup intentionally prevents an ambiguous second migration.

### Migration rollback

1. Stop the v0.2 stack.
2. Preserve the current `STATE_DIR/settings.db` for diagnosis.
3. Copy `STATE_DIR/settings.db.v01.bak` back to `ZIM_DIR/settings.db`.
4. Restore the v0.1 application revision and Compose definition.

The v0.2 secret files can remain on disk during rollback. Do not copy or commit
their values into `.env`, issue reports, or logs.

## Download policy

The admin UI accepts only server-issued, session-bound download actions. An
action is HMAC-signed, expires after 15 minutes, and can be used once. The worker
then enforces all of the following:

- HTTPS on `download.kiwix.org` or its subdomains only;
- no credentials, fragments, IP literals, nonstandard ports, or off-domain
  redirects;
- a known catalog size no larger than 200 GiB by default;
- free space for the artifact plus the greater of 2 GiB or 5%;
- one active download, a unique staging file, and no implicit replacement;
- response size agreement with the catalog; and
- successful `libzim` metadata access before atomic installation.

Operational errors shown in the UI are sanitized and omit internal URLs and
low-level exception details.

### Durable download operations

Download jobs are stored in the state database with their expected and received
sizes, server validators, timestamps, status, and job-specific staging path.
Stopping or restarting the admin service changes interrupted work to `paused`;
the Downloads page then offers Resume, Retry from start, Cancel, and Remove as
appropriate.

A resume sends `Range` and `If-Range` using the saved ETag or Last-Modified
validator. The worker accepts only an exact `206 Content-Range` beginning at the
staged file size and agreeing with the catalog total. A changed validator or
mismatched range leaves the partial file unmodified so the operator can retry
from the beginning. Graceful admin shutdown cancels and awaits workers after
persisting them as paused. Completed history is retained according to
`DOWNLOAD_HISTORY_RETENTION` (default 100).

## Resource boundaries

All public search result limits clamp to `1..20`; retrieval candidate settings
are restricted to `1..100`; article windows are restricted to `500..16000`
characters; and article offsets must be nonnegative and bounded. Blank or
oversized queries are rejected before any upstream request. JSON, XML, and HTML
responses are size-checked before parsing and malformed payloads return stable
tool errors instead of escaping through the MCP transport.

Each public tool has an independent concurrency limit. Waiting calls do not
start more upstream I/O. `calculate` additionally limits expressions to 4 KiB,
128 AST nodes, bounded nesting, operands, estimated result bits, function arity,
execution time, and formatted output size.

## Development and verification

Create a virtual environment, install runtime and development dependencies, and
run:

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check
.\scripts\compose.ps1 config --quiet
```

The tests cover credential validation, MCP bearer authentication, admin sessions
and CSRF, signed action tamper/replay/expiry, downloader SSRF and integrity,
restart/resume/cancel/retry/shutdown behavior, bounded tool inputs and
calculation, malformed upstream responses, read-only state access, migration
safety, and Compose trust boundaries.

## Configuration reference

Use `config/.env.example` for non-secret values and `config/secrets/*.example`
for the secret-file names. A direct credential variable and its `_FILE` variant
are mutually exclusive. Hosted credentials must contain at least 32 characters,
must not be known placeholders, and must all differ.

`EMBED_URL` and `RERANK_URL` are the endpoints used by host-side Python
commands. `CONTAINER_EMBED_URL` and `CONTAINER_RERANK_URL` are the endpoints
reachable from Docker; they default to `host.docker.internal` on ports 8081 and
8082. Explicitly blank container values disable the corresponding optional
service. The stack always uses its internal Kiwix/Qdrant service addresses.
When `KIWIX_PUBLIC_URL` is blank, Compose derives citation links from `KIWIX_PORT`.

Set `EMBED_MODEL_REVISION` to the weights revision when replacing a model under
the same name. Ingest uses it to invalidate old embeddings.

Important paths:

- `ZIM_DIR`: corpus and generated `library.xml`;
- `STATE_DIR`: runtime `settings.db` owned by the management plane;
- `STATE_DB`: incremental ingest manifest;
- `QDRANT_STORAGE`: vector database storage.

## Retrieval quality

Tools return MCP `structuredContent` and identical REST JSON (stable id, citation,
source kind, corpus version, retrieval/rerank scores) alongside the readable MCP
text fallback, so a client never has to parse prose to recover a citation.
Retrieved passages are untrusted source material: quote and cite them, never
follow them. `article_find` excludes bibliography/reference chunks from evidence
ranking while `article_read` still permits sequential access to the complete
extracted document.

Ingest requires a stable unique `id` per source in `ingest/sources.yaml`, and
records a per-file `status`/`reason` in the manifest so skipped and errored
files are visible rather than silently missing.

Ingest keys files and chunks by stable source ID and relative path. Moving a
source root preserves its index; changing its display label updates citations
without changing point identity. Relative source roots resolve from the parent
of the sources file's directory (the repo root for `ingest/sources.yaml`).

Missing roots and incomplete scans preserve the last good index and report an
error. Sources removed from YAML are retained until explicitly removed:

```bash
python -m ingest.ingest --remove-source old-source-id
```

Remove the matching YAML entry first. The command removes only that source's
indexed points and manifest entries; it never deletes original source files.
A normal successful scan still removes entries for individually deleted files.
A source with file errors defers deletions until its next healthy scan.

Ingest fingerprints the model name/revision, dimensions, chunk settings, pipeline
version, source ID, and collection. Changed fingerprints trigger re-embedding.
It also checks the actual stored points before skipping an unchanged file, so
recreated collections, missing points, and stale restored points are repaired.
Use `python -m ingest.ingest --rebuild` to force re-embedding of available files.
Collection dimension/distance mismatches fail before ingest writes; choose a new
`QDRANT_COLLECTION` and a separate `STATE_DB` for a staged rebuild, ingest into it,
then switch the gateway to the new collection.
Keep the old collection until the replacement is verified.

The first run upgrades older manifests transactionally and saves a non-overwriting
`<STATE_DB>.v1.bak` backup. Keep existing source labels for that first run so old
entries can be mapped to stable IDs; rename labels afterward. A rollback requires
restoring that manifest backup and the matching Qdrant backup together.
First-time file failures are persisted, and runs with errors exit nonzero.
Failed files keep their last good vectors and fingerprint for retry; a failed
model upgrade can therefore retain old-model vectors until the next successful
run. Do not switch a production gateway to replacement weights until ingest
succeeds; use a new collection for a staged model upgrade.

Retrieval changes are gated on measurements, not judgement:

```bash
python -m ingest.ingest                        # index the curated corpus
python -m evaluation.run_eval                  # recall@k, MRR, nDCG, citation
                                               # correctness, duplicates, latency
python -m evaluation.run_eval --update-baseline  # record the current numbers
python -m evaluation.run_eval --check          # fail if quality regressed
```

The dataset (`evaluation/datasets/retrieval_v1.yaml`) is versioned; the baseline
records the dataset version it was measured against and refuses to compare
across versions. Latency is reported but not gated, since it depends on the
machine rather than on retrieval quality.

## Next phases

Phase 4 adds reproducible dependency/image pinning, non-root/read-only containers,
structured logs and metrics, migration backups, stronger CI/release gates, and
broader service lifecycle work. Multi-user administration and automated TLS
remain deployment features beyond this single-workstation v0.2 scope.
