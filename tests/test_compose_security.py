"""Static assertions for the Compose trust boundaries."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _compose():  # noqa: ANN202
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_all_published_ports_are_loopback_bound():
    services = _compose()["services"]
    for name in ("gateway", "admin", "kiwix", "qdrant"):
        ports = services[name].get("ports", [])
        assert ports, f"{name} should publish its operational host port"
        assert all(str(port).startswith("127.0.0.1:") for port in ports)


def test_corpus_and_state_mount_permissions_match_service_boundaries():
    services = _compose()["services"]
    assert any("/corpus:ro" in volume for volume in services["gateway"]["volumes"])
    assert any("/state:ro" in volume for volume in services["gateway"]["volumes"])
    assert any("/data:ro" in volume for volume in services["kiwix"]["volumes"])
    admin_volumes = services["admin"]["volumes"]
    assert any("/corpus" in volume and not volume.endswith(":ro") for volume in admin_volumes)
    assert any("/state" in volume and not volume.endswith(":ro") for volume in admin_volumes)
    assert "secrets" not in services["kiwix"]
    assert all("/state" not in volume for volume in services["kiwix"]["volumes"])


def test_networks_isolate_clients_from_backend_services():
    services = _compose()["services"]
    assert "las-clients" in services["gateway"]["networks"]
    assert "las-clients" not in services["admin"]["networks"]
    for name in ("kiwix", "qdrant"):
        assert "las-backend" in services[name]["networks"]
        assert "las-clients" not in services[name]["networks"]
    assert _compose()["networks"]["las-backend"]["internal"] is True


def test_state_and_config_validation_complete_before_hosted_services():
    services = _compose()["services"]
    assert services["state-init"]["depends_on"]["config-validator"]["condition"] == (
        "service_completed_successfully"
    )
    for name in ("gateway", "admin"):
        assert services[name]["depends_on"]["state-init"]["condition"] == (
            "service_completed_successfully"
        )


def test_legacy_mcpo_has_no_host_port_and_uses_secret_files():
    legacy = yaml.safe_load(
        (ROOT / "docker-compose.legacy-mcpo.yml").read_text(encoding="utf-8")
    )["services"]["mcpo"]
    assert "ports" not in legacy
    assert legacy["profiles"] == ["legacy-mcpo"]
    assert set(legacy["secrets"]) == {"mcp_api_key", "mcpo_api_key"}
