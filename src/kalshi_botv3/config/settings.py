from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Kalshi API credentials — required in all modes except dry_run
    kalshi_api_key_id: str

    # Path to the RSA private key PEM file used to sign Kalshi API requests
    kalshi_private_key_path: Path

    # Target Kalshi environment: "demo" for paper trading, "prod" for real money
    kalshi_env: Literal["demo", "prod"] = "demo"

    # Execution mode: dry_run=no orders, paper=simulated fills, live=real orders
    mode: Literal["dry_run", "paper", "live"] = "dry_run"

    # Async SQLite connection URL for the local database
    database_url: str = "sqlite+aiosqlite:///./data/bot.db"

    # Structlog log level
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # Port the FastAPI health/dashboard server listens on
    health_port: int = 8080

    # Path to the runtime YAML config file
    config_path: Path = Path("src/kalshi_botv3/config/config.yaml")

    @model_validator(mode="after")
    def _validate_key_path(self) -> "Settings":
        """Private key must exist unless mode is dry_run."""
        if self.mode != "dry_run" and not self.kalshi_private_key_path.exists():
            raise ValueError(
                f"kalshi_private_key_path '{self.kalshi_private_key_path}' does not exist. "
                "The key file is required for paper and live modes."
            )
        return self

    @model_validator(mode="after")
    def _validate_live_mode(self) -> "Settings":
        """Live mode requires prod env, a real API key, and a readable key file."""
        if self.mode != "live":
            return self
        if self.kalshi_env != "prod":
            raise ValueError(
                "live mode requires kalshi_env='prod' — set KALSHI_ENV=prod."
            )
        if not self.kalshi_api_key_id:
            raise ValueError(
                "live mode requires a non-empty kalshi_api_key_id."
            )
        if not self.kalshi_private_key_path.exists():
            raise ValueError(
                f"live mode requires a readable private key file; "
                f"'{self.kalshi_private_key_path}' does not exist."
            )
        try:
            self.kalshi_private_key_path.read_bytes()
        except OSError as exc:
            raise ValueError(
                f"live mode: cannot read private key at "
                f"'{self.kalshi_private_key_path}': {exc}"
            ) from exc
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
