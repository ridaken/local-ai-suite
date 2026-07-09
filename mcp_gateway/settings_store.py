"""Runtime settings store (sqlite) for the admin UI's toggles.

Split deliberately from config/.env: `.env` holds secrets and paths (read once
at process start), while this store holds behavior a human flips at runtime
through the admin UI — retrieval mode, reranking, and which offline books
participate in kb_search. Every read goes straight to sqlite so a toggle takes
effect on the *next* request, no gateway restart required.

A `SettingsStore` is constructed with an explicit path so tests can point it at
a temp file; `default_store()` returns the process-wide instance backed by
`config.SETTINGS_DB`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from threading import Lock

RETRIEVAL_MODES = ("hybrid", "lexical", "vector")
DEFAULT_RETRIEVAL_MODE = "hybrid"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS book_toggles (
    name TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL
);
"""


class SettingsStore:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    # --- retrieval mode ------------------------------------------------------
    def get_retrieval_mode(self) -> str:
        value = self._get("retrieval_mode")
        return value if value in RETRIEVAL_MODES else DEFAULT_RETRIEVAL_MODE

    def set_retrieval_mode(self, mode: str) -> None:
        if mode not in RETRIEVAL_MODES:
            raise ValueError(
                f"unknown retrieval mode: {mode!r} (expected one of {RETRIEVAL_MODES})"
            )
        self._set("retrieval_mode", mode)

    # --- reranking -------------------------------------------------------------
    def get_rerank_enabled(self) -> bool:
        value = self._get("rerank_enabled")
        return value != "0"  # default on

    def set_rerank_enabled(self, enabled: bool) -> None:
        self._set("rerank_enabled", "1" if enabled else "0")

    # --- per-book toggles --------------------------------------------------
    def is_book_enabled(self, name: str) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT enabled FROM book_toggles WHERE name = ?", (name,)
            ).fetchone()
        return True if row is None else bool(row[0])  # default: enabled

    def set_book_enabled(self, name: str, enabled: bool) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO book_toggles (name, enabled) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET enabled = excluded.enabled",
                (name, int(enabled)),
            )

    def book_toggles(self) -> dict[str, bool]:
        """Only explicit overrides — books absent here default to enabled."""
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT name, enabled FROM book_toggles").fetchall()
        return {name: bool(enabled) for name, enabled in rows}

    def enabled_books(self, installed: list[str]) -> list[str]:
        """Filter an installed-book-name list down to the enabled ones."""
        return [name for name in installed if self.is_book_enabled(name)]

    # --- internal --------------------------------------------------------------
    def _get(self, key: str) -> str | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def _set(self, key: str, value: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )


_default: SettingsStore | None = None


def default_store() -> SettingsStore:
    global _default
    if _default is None:
        from . import config

        _default = SettingsStore(config.SETTINGS_DB)
    return _default
