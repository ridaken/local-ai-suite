"""Fail-closed hosted configuration validation used by Docker Compose."""

from . import config


def main() -> None:
    config.validate_http_security(admin=True, mcp=True)
    config.validate_runtime_limits()
    print("hosted security configuration is valid")


if __name__ == "__main__":
    main()
