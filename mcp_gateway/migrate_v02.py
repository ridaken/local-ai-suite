"""Dry-run-by-default migration from the v0.1 corpus-side settings database."""

from __future__ import annotations

import argparse
import contextlib
import secrets
import shutil
import sqlite3
from pathlib import Path

from . import config
from .settings_store import SettingsStore

_SECRET_FILES = {
    "KAGI_API_KEY": "kagi_api_key.txt",
    "NCBI_API_KEY": "ncbi_api_key.txt",
}
_GENERATED_SECRET_FILES = {
    "ADMIN_TOKEN": "admin_token.txt",
    "MCP_API_KEY": "mcp_api_key.txt",
}


def _secret_file_has_value(path: Path) -> bool:
    if not path.exists():
        return False
    if not path.is_file():
        raise ValueError(f"secret path must be a file: {path}")
    return bool(path.read_text(encoding="utf-8").strip())


def _write_secret(path: Path, value: str) -> None:
    if path.exists() and not path.is_file():
        raise ValueError(f"secret path must be a file: {path}")
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)


def migrate(
    legacy_db: Path,
    state_dir: Path,
    secret_dir: Path,
    *,
    apply: bool = False,
) -> list[str]:
    actions: list[str] = []
    if not legacy_db.is_file():
        return [f"no legacy database found at {legacy_db}"]
    target = state_dir / "settings.db"
    backup = state_dir / "settings.db.v01.bak"
    if legacy_db.resolve() == target.resolve():
        raise ValueError("legacy and target settings paths must differ")
    if backup.exists():
        raise FileExistsError(f"migration backup already exists at {backup}")

    with contextlib.closing(sqlite3.connect(legacy_db)) as conn:
        settings = dict(conn.execute("SELECT key, value FROM settings").fetchall())
        toggles = conn.execute("SELECT name, enabled FROM book_toggles").fetchall()

    actions.append(f"back up legacy database to {backup}")
    actions.append(f"copy behavioral settings into {target}")
    for key, filename in _SECRET_FILES.items():
        value = settings.get(f"config.{key}", "")
        destination = secret_dir / filename
        if value and not _secret_file_has_value(destination):
            actions.append(f"write configured {key} to {destination}")
        elif value:
            actions.append(f"keep existing secret file for {key} at {destination}")
    for key, filename in _GENERATED_SECRET_FILES.items():
        destination = secret_dir / filename
        if _secret_file_has_value(destination):
            actions.append(f"keep existing secret file for {key} at {destination}")
        else:
            actions.append(f"generate a strong {key} in {destination}")
    actions.append(f"remove legacy database from {legacy_db} after verification")
    if not apply:
        return actions

    state_dir.mkdir(parents=True, exist_ok=True)
    secret_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(legacy_db, backup)
    target_store = SettingsStore(target)
    mode = settings.get("retrieval_mode")
    if mode:
        target_store.set_retrieval_mode(mode)
    rerank = settings.get("rerank_enabled")
    if rerank is not None:
        target_store.set_rerank_enabled(rerank != "0")
    for name, enabled in toggles:
        target_store.set_book_enabled(str(name), bool(enabled))
    for key, filename in _SECRET_FILES.items():
        value = settings.get(f"config.{key}", "")
        destination = secret_dir / filename
        if value and not _secret_file_has_value(destination):
            _write_secret(destination, value)
    for filename in _GENERATED_SECRET_FILES.values():
        destination = secret_dir / filename
        if not _secret_file_has_value(destination):
            _write_secret(destination, secrets.token_urlsafe(48))
    if not backup.is_file() or not target.is_file():
        raise RuntimeError("migration verification failed; legacy database was not removed")
    legacy_db.unlink()
    return actions


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    default_legacy = Path(config.ZIM_DIR) / "settings.db" if config.ZIM_DIR else Path("settings.db")
    parser.add_argument("--legacy-db", type=Path, default=default_legacy)
    parser.add_argument("--state-dir", type=Path, default=Path(config.STATE_DIR))
    parser.add_argument("--secret-dir", type=Path, default=Path("config/secrets"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    actions = migrate(
        args.legacy_db, args.state_dir, args.secret_dir, apply=args.apply
    )
    print("migration applied" if args.apply else "dry run; pass --apply to make changes")
    for action in actions:
        print(f"- {action}")


if __name__ == "__main__":
    main()
