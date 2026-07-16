"""State isolation, initialization ordering, and v0.1 migration tests."""

import contextlib
import sqlite3

import pytest

from mcp_gateway import config, state_init
from mcp_gateway.migrate_v02 import migrate
from mcp_gateway.settings_store import SettingsStore


def _legacy_db(path):  # noqa: ANN001, ANN202
    store = SettingsStore(path)
    store.set_retrieval_mode("lexical")
    store.set_rerank_enabled(False)
    store.set_book_enabled("wikipedia_en_all", False)
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.executemany(
            "INSERT INTO settings(key, value) VALUES (?, ?)",
            [
                ("config.KAGI_API_KEY", "kagi-super-secret"),
                ("config.NCBI_API_KEY", "ncbi-super-secret"),
                ("config.ZIM_DIR", "legacy-path"),
            ],
        )
        conn.commit()
    return path


def test_migration_is_dry_run_by_default_and_never_reports_values(tmp_path):
    legacy = _legacy_db(tmp_path / "corpus" / "settings.db")
    state = tmp_path / "state"
    secrets = tmp_path / "secrets"

    actions = migrate(legacy, state, secrets)

    report = "\n".join(actions)
    assert "kagi-super-secret" not in report
    assert "ncbi-super-secret" not in report
    assert legacy.exists()
    assert not state.exists()
    assert not secrets.exists()


def test_migration_backs_up_copies_behavior_and_isolates_secrets(tmp_path):
    legacy = _legacy_db(tmp_path / "corpus" / "settings.db")
    state = tmp_path / "state"
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "kagi_api_key.txt").write_text("keep-this", encoding="utf-8")

    migrate(legacy, state, secrets, apply=True)

    assert not legacy.exists()
    assert (state / "settings.db.v01.bak").is_file()
    migrated = SettingsStore(state / "settings.db", read_only=True, initialize=False)
    assert migrated.get_retrieval_mode() == "lexical"
    assert migrated.get_rerank_enabled() is False
    assert migrated.is_book_enabled("wikipedia_en_all") is False
    assert migrated.get_config_value("KAGI_API_KEY") is None
    assert (secrets / "kagi_api_key.txt").read_text(encoding="utf-8") == "keep-this"
    assert (secrets / "ncbi_api_key.txt").read_text(encoding="utf-8").strip() == (
        "ncbi-super-secret"
    )
    admin_token = (secrets / "admin_token.txt").read_text(encoding="utf-8").strip()
    mcp_key = (secrets / "mcp_api_key.txt").read_text(encoding="utf-8").strip()
    assert len(admin_token) >= 32
    assert len(mcp_key) >= 32
    assert admin_token != mcp_key


def test_migration_failure_does_not_remove_legacy_database(tmp_path, monkeypatch):
    legacy = _legacy_db(tmp_path / "corpus" / "settings.db")

    def fail(_self, _mode):  # noqa: ANN001
        raise RuntimeError("simulated target failure")

    monkeypatch.setattr(SettingsStore, "set_retrieval_mode", fail)
    with pytest.raises(RuntimeError, match="simulated"):
        migrate(legacy, tmp_path / "state", tmp_path / "secrets", apply=True)

    assert legacy.exists()
    assert (tmp_path / "state" / "settings.db.v01.bak").exists()


def test_state_initialization_refuses_legacy_corpus_database(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "settings.db").write_bytes(b"legacy")
    monkeypatch.setattr(config, "ZIM_DIR", str(corpus))
    monkeypatch.setattr(config, "SETTINGS_DB", str(tmp_path / "state" / "settings.db"))

    with pytest.raises(RuntimeError, match="migration"):
        state_init.initialize_state()

    assert not (tmp_path / "state" / "settings.db").exists()


def test_state_initialization_precedes_read_only_mcp_access(tmp_path, monkeypatch):
    target = tmp_path / "state" / "settings.db"
    monkeypatch.setattr(config, "ZIM_DIR", str(tmp_path / "corpus"))
    monkeypatch.setattr(config, "SETTINGS_DB", str(target))

    with pytest.raises(sqlite3.OperationalError):
        SettingsStore(target, read_only=True, initialize=False).get_retrieval_mode()

    state_init.initialize_state()
    read_only = SettingsStore(target, read_only=True, initialize=False)
    assert read_only.get_retrieval_mode() == "hybrid"
    with pytest.raises(PermissionError):
        read_only.set_retrieval_mode("lexical")
