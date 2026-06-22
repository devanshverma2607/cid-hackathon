"""Application settings loaded from the environment (.env via pydantic-settings)."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed view over every environment variable used by the stack."""

    # Runtime
    app_env: str = "development"
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # Credentials
    postgres_password: str = "changeme_in_prod"
    neo4j_password: str = "changeme_in_prod"
    minio_user: str = "socmint"
    minio_password: str = "changeme_in_prod"

    # Connection strings
    redis_url: str = "redis://redis:6379/0"
    database_url: str = "postgresql://socmint:changeme_in_prod@postgres:5432/socmint"
    neo4j_uri: str = "bolt://neo4j:7687"
    minio_endpoint: str = "minio:9000"

    # Tool API keys
    h8mail_api_key: str = ""
    hibp_api_key: str = ""
    emailrep_api_key: str = ""
    censys_api_id: str = ""
    censys_api_secret: str = ""
    dnsdumpster_api_key: str = ""
    picarta_api_key: str = ""
    ai_geolocation_enabled: str = "0"
    reddit_client_id: str = ""
    reddit_client_secret: str = ""

    # Platform tokens
    instagram_session_id: str = ""
    ghunt_cookies_path: str = "/tools/python/ghunt/cookies.json"

    # Proxy
    tor_proxy: str = "socks5://127.0.0.1:9050"

    # Object storage
    minio_bucket: str = "socmint-evidence"
    minio_secure: bool = False

    # Local case output directory (mounted volume)
    cases_dir: str = "/app/cases"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()
