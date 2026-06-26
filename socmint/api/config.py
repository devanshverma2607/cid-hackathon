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
    # JWT / Authentication
    jwt_secret_key: str = "CHANGE-ME-IN-PRODUCTION"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 480  # 8-hour analyst session
    # Bootstrap admin — created on first startup when users table is empty
    socmint_admin_email: str = ""
    socmint_admin_password: str = ""
    socmint_admin_username: str = "admin"
    # Google OAuth 2.0
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/api/v1/auth/google/callback"
    # Where to redirect the browser after a successful Google login
    frontend_url: str = "http://localhost:8501"


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
