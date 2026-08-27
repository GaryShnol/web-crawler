"""Runtime configuration, loaded from environment variables (and .env for local runs)."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # None only ever reaches run(), which requires it before crawling --
    # `stats` never touches config.seed_url, so it has nothing to fake.
    seed_url: str | None = None
    database_url: str
    fetch_api_url: str

    max_concurrency: int = 8
    max_depth: int | None = None
    requests_per_second: float = 5.0
    max_attempts: int = 5
    lease_seconds: int = 120
    max_body_bytes: int = 52_428_800
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 30.0
    allow_subdomains: bool = False

    # None means "half of lease_seconds" — a fixed literal default would
    # silently drift out of sync with a lease_seconds override.
    lease_recovery_interval_seconds: float | None = None
    # Separate from lease recovery's cadence on purpose: noticing an empty
    # frontier is a cheap existence check, not a lease-safety window, so it
    # shouldn't wait on how long a lease is allowed to go stale (see
    # DESIGN.md).
    drain_check_interval_seconds: float = 2.0
    poll_interval_seconds: float = 1.0
    shutdown_grace_seconds: float = 30.0
    progress_interval_seconds: float = 10.0

    rate_limit_min_rps: float = 0.5
    rate_limit_decrease_factor: float = 0.5
    rate_limit_recovery_successes: int = 10
    rate_limit_increase_rps: float = 0.5

    retry_base_seconds: float = 1.0
    retry_max_seconds: float = 60.0
    output_dir: Path = Path("output")
    log_level: str = "INFO"
