# local-ai-suite

local-ai-suite is a local-first MCP server for offline Kiwix search, hybrid
Qdrant retrieval, web and research tools, and basic utility tools. Version 0.2
splits the MCP data plane from the administrator control plane and fails closed
when hosted credentials or state are missing.

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
and CSRF, signed action tamper/replay/expiry, downloader SSRF and integrity
checks, read-only state access, migration safety, and Compose trust boundaries.

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

## Known v0.2 limitations

Phase 2 work remains for durable download job persistence/resume, broader
container CPU and memory limits, image and dependency digest pinning, audit-log
retention, multi-user administration, automated certificate management, and
more extensive observability. The v0.2 safeguards intentionally cover the
minimum required to close the Phase 1 security boundary without expanding into
those operational features.
