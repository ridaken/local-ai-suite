"""Configuration loaded from environment / config/.env.

Kept deliberately tiny: read once at import, expose plain module-level values.
All data locations and endpoints are configurable so nothing is hard-coded to a
particular machine (see config/.env.example).
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load config/.env if present (repo_root/config/.env). Real environment
# variables always win over the file, so container/CI overrides work.
_CONFIG_ENV = Path(__file__).resolve().parent.parent / "config" / ".env"
load_dotenv(_CONFIG_ENV, override=False)


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


# Kiwix (offline KB, full-text search)
KIWIX_URL = os.environ.get("KIWIX_URL", "http://localhost:8080").rstrip("/")
KIWIX_BOOK = os.environ.get("KIWIX_BOOK", "").strip()

# Web search (Kagi)
KAGI_API_KEY = os.environ.get("KAGI_API_KEY", "").strip()
KAGI_SEARCH_URL = os.environ.get("KAGI_SEARCH_URL", "https://kagi.com/api/v0/search").strip()

# PubMed (NCBI E-utilities)
NCBI_API_KEY = os.environ.get("NCBI_API_KEY", "").strip()
NCBI_EMAIL = os.environ.get("NCBI_EMAIL", "").strip()
NCBI_TOOL = os.environ.get("NCBI_TOOL", "local-ai-suite").strip()
NCBI_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

# arXiv
ARXIV_API_URL = os.environ.get("ARXIV_API_URL", "https://export.arxiv.org/api/query").strip()

# Defaults
KB_SEARCH_LIMIT = _int("KB_SEARCH_LIMIT", 5)
WEB_SEARCH_LIMIT = _int("WEB_SEARCH_LIMIT", 5)

# Shared HTTP settings
HTTP_TIMEOUT = 20.0
USER_AGENT = f"local-ai-suite/{os.environ.get('LAS_VERSION', '0.1.0')} (mcp-gateway)"


# --- Phase 2: semantic retrieval ---------------------------------------------
# Vector DB. QDRANT_URL is used by the compose service; leave blank to disable the
# vector tier entirely (kb_search then falls back to Kiwix-only lexical search).
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333").strip()
QDRANT_COLLECTION = os.environ.get("QDRANT_COLLECTION", "las_curated").strip()

# Embeddings + reranker served by llama-server (OpenAI-compatible endpoints).
# Blank EMBED_URL also disables the vector tier.
EMBED_URL = os.environ.get("EMBED_URL", "http://localhost:8081/v1/embeddings").strip()
EMBED_MODEL = os.environ.get("EMBED_MODEL", "bge-m3").strip()
EMBED_DIM = _int("EMBED_DIM", 1024)  # bge-m3 = 1024
RERANK_URL = os.environ.get("RERANK_URL", "http://localhost:8082/v1/rerank").strip()
RERANK_MODEL = os.environ.get("RERANK_MODEL", "bge-reranker-v2-m3").strip()

# Retrieval sizing: how many candidates each source contributes before reranking,
# and how many survive to the answer.
HYBRID_VECTOR_CANDIDATES = _int("HYBRID_VECTOR_CANDIDATES", 20)
HYBRID_KIWIX_CANDIDATES = _int("HYBRID_KIWIX_CANDIDATES", 20)

# Ingest / chunking. Sizes are in characters (~4 chars per token) to avoid a
# tokenizer dependency; STATE_DB is the incremental manifest.
CHUNK_MAX_CHARS = _int("CHUNK_MAX_CHARS", 2000)
CHUNK_OVERLAP_CHARS = _int("CHUNK_OVERLAP_CHARS", 200)
STATE_DB = os.environ.get(
    "STATE_DB", str(Path(__file__).resolve().parent.parent / "ingest" / "state.db")
).strip()


def vector_tier_enabled() -> bool:
    """The vector tier is active only if both a vector DB and an embedder are set."""
    return bool(QDRANT_URL and EMBED_URL)


# --- Phase 3: management plane -------------------------------------------------
# Runtime toggles (retrieval mode, rerank on/off, per-book enable/disable) live
# here, separate from state.db (the ingest manifest).
SETTINGS_DB = os.environ.get(
    "SETTINGS_DB", str(Path(__file__).resolve().parent.parent / "ingest" / "settings.db")
).strip()

# Where downloaded ZIMs land and where library.xml is generated for kiwix's
# --library --monitorLibrary mode. Defaults to ZIM_DIR itself (one bind mount
# serves both purposes: kiwix reads it read-only, the gateway writes to it).
ZIM_DIR = os.environ.get("ZIM_DIR", "").strip()

# The path kiwix-serve sees ZIM_DIR at inside its own container (used when
# writing the "path" attribute into library.xml). Only relevant when both
# services run under docker-compose with the standard bind mount.
KIWIX_DATA_DIR = os.environ.get("KIWIX_DATA_DIR", "/data").strip()

# Where the gateway writes library.xml (and reads it back to know which book
# names are installed, for the per-book enable/disable toggle). Defaults under
# ZIM_DIR since kiwix's --library --monitorLibrary mode needs it on the same
# bind mount kiwix already reads. Left unset when ZIM_DIR is unset — retrieval
# then skips book filtering entirely (safe: same as pre-Phase-3 behavior).
#
# Note: blank (not just absent) falls through to the computed default —
# os.environ.get()'s default only applies when the key is missing, but
# docker-compose's env_file turns config/.env's documented-blank
# `LIBRARY_XML_PATH=` line into an actual empty-string env var.
LIBRARY_XML_PATH = os.environ.get("LIBRARY_XML_PATH", "").strip() or (
    f"{ZIM_DIR}/library.xml" if ZIM_DIR else ""
)

# Admin UI + MCP-over-HTTP bind address. Loopback by default — LAN exposure is
# an explicit opt-in, not the default, since the admin UI has no auth of its own.
ADMIN_HOST = os.environ.get("ADMIN_HOST", "127.0.0.1").strip()
ADMIN_PORT = _int("ADMIN_PORT", 8090)

# Kiwix OPDS catalog (browse available ZIMs to download).
KIWIX_CATALOG_URL = os.environ.get(
    "KIWIX_CATALOG_URL", "https://library.kiwix.org/catalog/v2/entries"
).strip()
