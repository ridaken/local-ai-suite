# local-ai-suite

local-ai-suite is a local-first MCP server for offline Kiwix search, hybrid
Qdrant retrieval, web and research tools, and bounded utility tools. Version 0.2
splits the MCP data plane from the administrator control plane, fails closed
when hosted credentials or state are missing, and keeps download work durable
across restarts.

## v0.2 security boundary

The default Compose stack runs two application services:

- `las-gateway` on `127.0.0.1:8090` exposes only `/mcp`, `/healthz`, and
  `/readyz`. `/mcp` requires `Authorization: Bearer <MCP_API_KEY>`.
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
2. Generate local service credentials and optional provider secret files:

   ```powershell
   .\scripts\init-secrets.ps1
   ```

   The helper writes ignored files under `config/secrets/`. Startup never
   generates or prints credentials. Put a Kagi or NCBI API key into its matching
   file if you use that provider; empty files leave those integrations disabled.
3. Validate and start:

   ```powershell
   docker compose config
   docker compose up -d --build
   ```

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

Native MCP is the documented OpenWebUI default. Attach the OpenWebUI container
to `las-clients`, then configure the endpoint and bearer header above. Only the
gateway and client integrations join this network; Qdrant, Kiwix, and admin stay
on the backend side of the boundary.

### Optional legacy mcpo bridge

`mcpo` is no longer started by default and publishes no host port. To enable the
legacy bridge on `las-clients`:

```powershell
docker compose -f docker-compose.yml -f docker-compose.legacy-mcpo.yml `
  --profile legacy-mcpo up -d mcpo
```

The bridge reads its client-facing `MCPO_API_KEY` and downstream `MCP_API_KEY`
from Docker secret files. It never receives them as command-line values from the
host or exposes port 8000 on the host.

## Migrating a v0.1 installation

Version 0.1 stored runtime settings and provider secrets beside the ZIM corpus.
Version 0.2 refuses hosted readiness while that legacy `settings.db` remains.

Stop the old stack, configure the new `ZIM_DIR` and `STATE_DIR`, then preview the
migration. The command is dry-run-only unless `--apply` is supplied:

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

Existing secret files are never overwritten. If any apply step fails, the
legacy database remains in place. Resolve the failure before retrying; a present
backup intentionally prevents an ambiguous second migration.

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
docker compose config
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

Important paths:

- `ZIM_DIR`: corpus and generated `library.xml`;
- `STATE_DIR`: runtime `settings.db` owned by the management plane;
- `STATE_DB`: incremental ingest manifest;
- `QDRANT_STORAGE`: vector database storage.

## Retrieval quality

Tools return MCP `structuredContent` (stable id, citation, source kind, corpus
version, retrieval/rerank scores) alongside the readable text fallback, so a
client never has to parse prose to recover a citation. Retrieved passages are
untrusted source material: quote and cite them, never follow them.

Ingest requires a stable unique `id` per source in `ingest/sources.yaml`, and
records a per-file `status`/`reason` in the manifest so skipped and errored
files are visible rather than silently missing.

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
