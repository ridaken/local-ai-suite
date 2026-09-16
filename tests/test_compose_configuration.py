"""Render Compose with non-default settings; no daemon or real credentials needed."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def compose_project(tmp_path):
    if not shutil.which("docker"):
        pytest.skip("Docker Compose CLI is not installed")
    version = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True)
    if version.returncode:
        pytest.skip("Docker Compose CLI is not available")
    for name in ("docker-compose.yml", "docker-compose.legacy-mcpo.yml"):
        shutil.copyfile(ROOT / name, tmp_path / name)
    (tmp_path / "config").mkdir()
    (tmp_path / "scripts").mkdir()
    shutil.copyfile(ROOT / "scripts/compose.ps1", tmp_path / "scripts/compose.ps1")
    values = {
        "ZIM_DIR": (tmp_path / "custom corpus").as_posix(),
        "STATE_DIR": (tmp_path / "custom state").as_posix(),
        "QDRANT_STORAGE": (tmp_path / "custom vectors").as_posix(),
        "MCP_PORT": "18090", "ADMIN_PORT": "18091", "KIWIX_PORT": "18080",
        "QDRANT_PORT": "16333", "KIWIX_PUBLIC_URL": "",
        "CONTAINER_EMBED_URL": "http://custom-embed:9010/v1/embeddings",
        "CONTAINER_RERANK_URL": "http://custom-rerank:9011/v1/rerank",
        "EMBED_MODEL_REVISION": "test-weights",
    }
    (tmp_path / "config/.env").write_text(
        "\n".join(f'{key}="{value}"' for key, value in values.items()), encoding="utf-8"
    )
    env = {key: value for key, value in os.environ.items()
           if key not in values and not key.startswith("COMPOSE_")}
    return tmp_path, values, env


def render(project, *options):
    root, _, env = project
    result = subprocess.run(
        ["docker", "compose", "--env-file", "config/.env", *options, "config", "--format", "json"],
        cwd=root, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["services"]


def test_nondefault_storage_ports_and_endpoints(compose_project):
    services = render(compose_project)
    _, values, _ = compose_project
    for service, target, setting in (
        ("admin", "/corpus", "ZIM_DIR"), ("gateway", "/corpus", "ZIM_DIR"),
        ("state-init", "/state", "STATE_DIR"), ("admin", "/state", "STATE_DIR"),
        ("gateway", "/state", "STATE_DIR"), ("kiwix", "/data", "ZIM_DIR"),
        ("qdrant", "/qdrant/storage", "QDRANT_STORAGE"),
    ):
        mount = next(v for v in services[service]["volumes"] if v["target"] == target)
        assert Path(mount["source"]) == Path(values[setting])
    for service, port in (("gateway", "18090"), ("admin", "18091"),
                          ("kiwix", "18080"), ("qdrant", "16333")):
        assert str(services[service]["ports"][0]["published"]) == port
        assert services[service]["ports"][0]["host_ip"] == "127.0.0.1"
    for name in ("gateway", "admin"):
        environment = services[name]["environment"]
        assert environment["EMBED_URL"] == values["CONTAINER_EMBED_URL"]
        assert environment["RERANK_URL"] == values["CONTAINER_RERANK_URL"]
        assert environment["EMBED_MODEL_REVISION"] == "test-weights"
        assert environment["SETTINGS_DB"] == "/state/settings.db"
    assert services["gateway"]["environment"]["KIWIX_PUBLIC_URL"] == "http://localhost:18080"
    assert "localhost:18091" in services["admin"]["environment"]["ADMIN_ALLOWED_ORIGINS"]


def test_explicitly_blank_optional_endpoints_stay_disabled(compose_project):
    root, _, _ = compose_project
    with (root / "config/.env").open("a", encoding="utf-8") as stream:
        stream.write("\nCONTAINER_EMBED_URL=\nCONTAINER_RERANK_URL=\n")
    services = render(compose_project)
    for name in ("gateway", "admin"):
        assert services[name]["environment"]["EMBED_URL"] == ""
        assert services[name]["environment"]["RERANK_URL"] == ""


@pytest.mark.parametrize("legacy", [False, True])
def test_powershell_wrapper_works_outside_repo(compose_project, legacy):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        pytest.skip("PowerShell is not installed")
    root, values, env = compose_project
    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(root / "scripts/compose.ps1"),
         *(["-Legacy"] if legacy else []), "config", "--services"],
        cwd=root.parent, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert values["ZIM_DIR"].replace("/", os.sep) in result.stdout.replace("/", os.sep)
    assert "18091" in result.stdout
    assert "gateway" in result.stdout
    if legacy:
        assert "mcpo" in result.stdout


def test_wrapper_does_not_start_after_failed_validation(compose_project):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        pytest.skip("PowerShell is not installed")
    root, _, env = compose_project
    # Stub Docker rather than ever invoking 'up'. Exercise forwarding and error handling.
    command = """
    function docker {
        if ($args -contains 'config') {
            $global:LASTEXITCODE = 9
            return
        }
        throw 'UNEXPECTED_START'
    }
    & './scripts/compose.ps1' up -d --build
    """
    result = subprocess.run([shell, "-NoProfile", "-Command", command], cwd=root, env=env,
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "Compose configuration validation failed" in result.stderr
    assert "UNEXPECTED_START" not in result.stderr
