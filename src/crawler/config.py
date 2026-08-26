"""Runtime configuration, loaded from environment variables (and .env for local runs)."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    seed_url: str
    database_url: str

    max_concurrency: int = 8
    max_depth: int | None = None
    requests_per_second: float = 5.0
    max_attempts: int = 5
    lease_seconds: int = 120
    max_body_bytes: int = 52_428_800
    max_redirects: int = 5
    allow_subdomains: bool = False
    output_dir: Path = Path("output")
    log_level: str = "INFO"
