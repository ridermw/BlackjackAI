from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


def _env_name(name: str) -> str:
    return f"BLACKJACK_AI_{name}"


def _read_str(name: str, default: str) -> str:
    return os.getenv(_env_name(name), default)


def _read_int(name: str, default: int) -> int:
    value = os.getenv(_env_name(name))
    if value is None:
        return default
    return int(value)


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str = "Blackjack AI Service"
    environment: str = "development"
    host: str = "127.0.0.1"
    port: int = 8000
    database_url: str = "sqlite:///blackjack_ai.db"

    @classmethod
    def from_env(cls) -> Settings:
        defaults = cls()
        return cls(
            app_name=_read_str("APP_NAME", defaults.app_name),
            environment=_read_str("ENVIRONMENT", defaults.environment),
            host=_read_str("HOST", defaults.host),
            port=_read_int("PORT", defaults.port),
            database_url=_read_str("DATABASE_URL", defaults.database_url),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
