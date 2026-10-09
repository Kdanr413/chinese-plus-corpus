"""Environment-variable configuration for PostgreSQL and Corpus Release storage."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values

from .errors import DatabaseConfigurationError


DATABASE_ENV_NAMES = ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")
CORPUS_DATA_ROOT_ENV = "CORPUS_DATA_ROOT"
PROJECT_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _configuration_environment(
    environment: Mapping[str, str] | None,
) -> Mapping[str, str]:
    """Load local .env defaults, while letting process variables take precedence."""
    if environment is not None:
        return environment
    file_values = {
        key: value or ""
        for key, value in dotenv_values(PROJECT_ENV_FILE).items()
        if value is not None
    }
    return {**file_values, **os.environ}


@dataclass(frozen=True)
class DatabaseSettings:
    host: str
    port: int
    name: str
    user: str
    password: str = field(repr=False)

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "DatabaseSettings":
        values = _configuration_environment(environment)
        host = values.get("DB_HOST", "127.0.0.1").strip() or "127.0.0.1"
        port_text = values.get("DB_PORT", "5432").strip() or "5432"
        missing = [
            name
            for name in ("DB_NAME", "DB_USER", "DB_PASSWORD")
            if not values.get(name, "").strip()
        ]
        if missing:
            raise DatabaseConfigurationError(
                "missing database environment variables: " + ", ".join(missing)
            )
        try:
            port = int(port_text)
        except ValueError as exc:
            raise DatabaseConfigurationError("DB_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise DatabaseConfigurationError("DB_PORT must be between 1 and 65535")
        return cls(
            host=host,
            port=port,
            name=values["DB_NAME"].strip(),
            user=values["DB_USER"].strip(),
            password=values["DB_PASSWORD"],
        )

    def connection_kwargs(self) -> dict[str, str | int]:
        return {
            "host": self.host,
            "port": self.port,
            "dbname": self.name,
            "user": self.user,
            "password": self.password,
        }


def corpus_data_root(
    explicit: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> Path:
    values = _configuration_environment(environment)
    raw = str(explicit or values.get(CORPUS_DATA_ROOT_ENV, "")).strip()
    if not raw:
        raise DatabaseConfigurationError(
            f"set {CORPUS_DATA_ROOT_ENV}; do not hard-code a Windows path in application code"
        )
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()
