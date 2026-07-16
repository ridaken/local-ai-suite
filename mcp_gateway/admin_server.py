"""Authenticated administration and download service."""

from __future__ import annotations

from pathlib import Path

from starlette.applications import Starlette

from . import config, zim_library
from .admin import build_admin_app
from .downloads import DownloadManager
from .settings_store import SettingsStore


def build_app() -> Starlette:
    config.validate_http_security(admin=True)
    settings = SettingsStore(config.SETTINGS_DB)

    def refresh_library() -> None:
        if config.ZIM_DIR and config.LIBRARY_XML_PATH:
            zim_library.refresh_library(Path(config.ZIM_DIR), Path(config.LIBRARY_XML_PATH))

    refresh_library()
    manager = DownloadManager(config.ZIM_DIR or ".", on_complete=refresh_library)
    return build_admin_app(
        settings=settings,
        download_manager=manager,
        zim_dir=config.ZIM_DIR,
        library_xml_path=config.LIBRARY_XML_PATH,
        admin_token=config.ADMIN_TOKEN,
    )


def main() -> None:
    import uvicorn

    uvicorn.run(build_app(), host=config.ADMIN_HOST, port=config.ADMIN_PORT)


if __name__ == "__main__":
    main()
