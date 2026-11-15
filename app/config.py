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
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = ""

    # Gmail OAuth 2.0
    GMAIL_CLIENT_ID: str = ""
    GMAIL_CLIENT_SECRET: str = ""
    GMAIL_REFRESH_TOKEN: str = ""

    # Notification & Personal Info
    MY_EMAIL: str = ""

    # Security
    CRON_SECRET: str = ""

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
