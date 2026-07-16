# Phase 2 operations and recovery

Phase 2 bounds public work and makes ZIM downloads restart-safe. It does not
change MCP authentication, admin login, storage mounts, or the v0.1-to-v0.2
migration contract introduced in Phase 1.

## Download state model

`state-init` creates a versioned `download_jobs` schema in `SETTINGS_DB` before
the admin service starts. Each record contains:

- job ID, validated catalog URL, destination filename, and staging path;
- expected, received, and response-total bytes;
- ETag and Last-Modified resume validators;
- queued/downloading/paused/cancelled/error/done status and sanitized error;
- created and updated timestamps.

Only `queued` and `downloading` are active. SQLite enforces one active record per
destination filename, while the manager also enforces the configured global
concurrency limit. Installed destinations are checked both before work begins
and during the atomic non-replacing installation.

## Restart and shutdown

At startup, any record left as queued/downloading is changed to paused and its
received-byte count is reconciled with the actual job-specific staging file.
During graceful ASGI shutdown, workers are marked paused, cancelled, and awaited.
The partial file is retained.

Use the Downloads page to:

- **Resume** a nonempty partial with `Range` and `If-Range`;
- **Retry from start** after deleting the partial and saved validators;
- **Cancel** active work while keeping a resumable partial; or
- **Remove** a non-active record and its partial file (never the installed ZIM).

Resume requires HTTP 206, an exact `Content-Range` start matching the local file,
the original catalog total, a consistent Content-Length, and unchanged validators.
Any mismatch is rejected before bytes are appended.

## Resource configuration

The defaults in `config/.env.example` are deliberately conservative:

- public result limits: 1..20;
- vector/Kiwix candidates: 1..100;
- article window: 500..16000 characters;
- upstream response body: 8 MiB;
- download concurrency: 1;
- tool concurrency: 4 for searches, 8 for reads/calculation;
- completed download history: 100 records.

Invalid integer syntax or out-of-range startup configuration fails validation
with the setting name. `CHUNK_OVERLAP_CHARS` must also remain smaller than
`CHUNK_MAX_CHARS`.

## Recovery guidance

If a resume reports a changed validator, invalid range, or an absent partial,
choose Retry from start. If disk space is insufficient, free enough space for
the remaining bytes plus the configured reserve before resuming. Never rename or
combine `.staging/*.part` files manually; the job ID and stored byte count are
part of the integrity check.

Back up `STATE_DIR` and the corpus together when preserving in-flight downloads.
The state database alone contains metadata, not the partial artifact; the corpus
alone contains a partial without its validated URL, size, and validators.
