import os
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # TiDB Serverless (MySQL Compatible)
    TIDB_HOST: str = ""
    TIDB_PORT: int = 4000
    TIDB_USER: str = ""
    TIDB_PASSWORD: str = ""
    TIDB_DATABASE: str = "gmail_summarizer"

    # Google Gemini API (Configured strictly via .env - No Hardcoding)
    # Default model per project rules: gemini-3.5-flash. Override via .env.
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-3.5-flash"

    # Encryption at rest for API keys stored in TiDB (see app/crypto.py).
    # Set to a long random value (e.g. Fernet key or any passphrase) in .env /
    # Render env vars. If unset, keys are stored as plaintext (legacy behaviour)
    # and a warning is logged — set it before adding keys in production.
    API_KEY_ENCRYPTION_KEY: str = ""

    # Gmail OAuth 2.0
    GMAIL_CLIENT_ID: str = ""
    GMAIL_CLIENT_SECRET: str = ""
    GMAIL_REFRESH_TOKEN: str = ""

    # Notification & Personal Info
    MY_EMAIL: str = ""

    # Security
    CRON_SECRET: str = ""

    # Admin token for the /api surface (see require_admin in app/routes/api.py).
    # Every /api route except /api/cron-trigger requires
    # `Authorization: Bearer <ADMIN_TOKEN>`. Set a long random value in
    # .env / Render env vars. If unset, the API fails CLOSED (503) rather
    # than serving mail and keys to the open internet.
    ADMIN_TOKEN: str = ""

    # Operational limits
    MAX_EMAILS_PER_RUN: int = 50
    PORT: int = 10000
    HOST: str = "0.0.0.0"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = Settings()
