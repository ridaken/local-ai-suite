"""Tests for the runtime settings store (retrieval mode, rerank toggle, and
per-book enable/disable) backing the admin UI."""

from mcp_gateway.settings_store import SettingsStore


def _store(tmp_path):
    return SettingsStore(tmp_path / "settings.db")


def test_retrieval_mode_defaults_to_hybrid(tmp_path):
    store = _store(tmp_path)
    assert store.get_retrieval_mode() == "hybrid"


def test_retrieval_mode_round_trips(tmp_path):
    store = _store(tmp_path)
    store.set_retrieval_mode("lexical")
    assert store.get_retrieval_mode() == "lexical"

    store.set_retrieval_mode("vector")
    assert store.get_retrieval_mode() == "vector"


def test_retrieval_mode_rejects_unknown_value(tmp_path):
    store = _store(tmp_path)
    try:
        store.set_retrieval_mode("bogus")
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_rerank_defaults_on(tmp_path):
    store = _store(tmp_path)
    assert store.get_rerank_enabled() is True


def test_rerank_toggle_round_trips(tmp_path):
    store = _store(tmp_path)
    store.set_rerank_enabled(False)
    assert store.get_rerank_enabled() is False
    store.set_rerank_enabled(True)
    assert store.get_rerank_enabled() is True


def test_book_defaults_enabled_when_untouched(tmp_path):
    store = _store(tmp_path)
    assert store.is_book_enabled("wikipedia_en_all") is True


def test_book_toggle_round_trips(tmp_path):
    store = _store(tmp_path)
    store.set_book_enabled("wikipedia_en_all", False)
    assert store.is_book_enabled("wikipedia_en_all") is False
    store.set_book_enabled("wikipedia_en_all", True)
    assert store.is_book_enabled("wikipedia_en_all") is True


def test_enabled_books_filters_installed_list(tmp_path):
    store = _store(tmp_path)
    store.set_book_enabled("stackoverflow", False)
    installed = ["wikipedia_en_all", "stackoverflow", "devdocs_python"]
    assert store.enabled_books(installed) == ["wikipedia_en_all", "devdocs_python"]


def test_settings_persist_across_instances(tmp_path):
    db_path = tmp_path / "settings.db"
    SettingsStore(db_path).set_retrieval_mode("lexical")
    assert SettingsStore(db_path).get_retrieval_mode() == "lexical"
