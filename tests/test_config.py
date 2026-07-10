from mcp_gateway import config


def test_apply_runtime_overrides_strips_string_values(monkeypatch):
    monkeypatch.setattr(config, "QDRANT_URL", "")

    config.apply_runtime_overrides({"QDRANT_URL": "  http://qdrant:6333  "})

    assert config.QDRANT_URL == "http://qdrant:6333"
