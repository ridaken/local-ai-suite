"""Initialize hosted service state before read-only MCP startup."""

from __future__ import annotations

from pathlib import Path

from . import config
from .downloads import DownloadJobStore
from .settings_store import SettingsStore


def initialize_state() -> None:
    legacy = Path(config.ZIM_DIR) / "settings.db" if config.ZIM_DIR else None
    target = Path(config.SETTINGS_DB)
    if legacy and legacy.exists() and legacy.resolve() != target.resolve():
        raise RuntimeError(
            f"legacy settings database remains at {legacy}; run the v0.2 migration before startup"
        )
    SettingsStore(target)
    DownloadJobStore(target)


def main() -> None:
    initialize_state()
    print(f"state initialized at {config.SETTINGS_DB}")


if __name__ == "__main__":
    main()
