"""Centralised application settings loaded from environment variables.

Uses ``pydantic-settings`` (Pydantic v2) so every value is typed, validated
and available through a single cached ``Settings`` instance.
"""

from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed application settings.

    All values are read from the process environment (or a local ``.env``
    file in development). Anything sensitive must be supplied via environment
    variables in production (Render dashboard, Docker secrets, etc.).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ---- Application ----
    APP_NAME: str = "Nupun Home Health Care Services"
    APP_ENV: str = "development"
    DEBUG: bool = True
    API_V1_PREFIX: str = "/api/v1"
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # ---- Security / JWT ----
    SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    PASSWORD_RESET_EXPIRE_MINUTES: int = 30

    # ---- Database ----
    MONGODB_URL: str
    MONGODB_DB_NAME: str = "nupun_health"

    # ---- Cloudinary ----
    CLOUDINARY_CLOUD_NAME: str = ""
    CLOUDINARY_API_KEY: str = ""
    CLOUDINARY_API_SECRET: str = ""
    CLOUDINARY_URL: str = ""

    # ---- Maps (Ola / Krutrim) — server-side only: geocoding + directions ----
    OLA_MAPS_API_KEY: str = ""

    # ---- Vision OCR (Google Gemini) — government-ID extraction, server-side ----
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-3.8-flash"

    # ---- Email (Resend & SMTP) ----
    RESEND_API_KEY: str = ""
    RESEND_FROM_EMAIL: str = "noreply@samiransamanta.in"
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = ""
    SMTP_FROM_NAME: str = "Srikrishna Seva Niketan"
    SMTP_TLS: bool = True
    SMTP_SSL: bool = False
    ADMIN_NOTIFICATION_EMAIL: str = ""

    # ---- Web Push Notifications (VAPID / RFC 8292) ----
    VAPID_PUBLIC_KEY: str = "BPr0MfGBuiqiXkN6GO9dGf-pl4abmE3azCXLdRrGNGjlt-fhR0ML1SfE9pMj14aq7dJL8qp8nrCgnpE0xF_UojA"
    VAPID_PRIVATE_KEY: str = "-----BEGIN PRIVATE KEY-----\nMIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgPMCRX5GOwKMj8Z6z\n+kAz8/O7aq5u+5i8qtSwNtP+I5mhRANCAAT69DHxgboqol5DehjvXRn/qZeGm5hN\n2swly3UaxjRo5bfn4UdDC9UnxPaTI9eGqu3SS/KqfJ6woJ6RNMRf1KIw\n-----END PRIVATE KEY-----\n"
    VAPID_CLAIM_EMAIL: str = "admin@shrikrishnanursing.com"

    # ---- Daily report scheduler ----
    ENABLE_SCHEDULER: bool = True
    REPORT_TIMEZONE: str = "Asia/Kolkata"
    DAILY_REPORT_HOUR: int = 20  # 24h local time the daily report is emailed

    # ---- CORS ----
    # Comma-separated string in the environment; use ``cors_origins`` for the
    # parsed list (avoids pydantic-settings JSON-decoding complex env values).
    CORS_ORIGINS: str = "*"

    # ---- Rate limiting ----
    RATE_LIMIT_DEFAULT: str = "200/minute"
    RATE_LIMIT_AUTH: str = "10/minute"

    # ---- Frontend ----
    FRONTEND_URL: str = "http://localhost:3000"

    # ---- Bootstrap admin ----
    FIRST_ADMIN_NAME: str = "Super Admin"
    FIRST_ADMIN_EMAIL: str = "admin@nupunhealth.com"
    FIRST_ADMIN_PASSWORD: str = "Admin@1234Secure!"

    @property
    def cors_origins(self) -> List[str]:
        """Return CORS origins parsed from the comma-separated string."""
        raw = self.CORS_ORIGINS.strip()
        defaults = [
            "https://srikrishna-seva-niketan-admin.vercel.app",
            "https://srikrishna-seva-niketan.vercel.app",
            "http://localhost:3000",
            "http://localhost:5173",
            "http://localhost:8000",
            "http://localhost:8080",
        ]
        if not raw or raw == "*":
            return defaults
        parsed = [origin.strip() for origin in raw.split(",") if origin.strip()]
        for d in defaults:
            if d not in parsed:
                parsed.append(d)
        return parsed

    @property
    def is_production(self) -> bool:
        """Return ``True`` when running in a production-like environment."""
        return self.APP_ENV.lower() in {"production", "prod"}

    @property
    def email_enabled(self) -> bool:
        """Return ``True`` when Resend or SMTP is configured."""
        if bool(self.RESEND_API_KEY):
            return True
        return bool(self.SMTP_HOST and self.SMTP_USER and self.SMTP_PASSWORD)

    @property
    def cloudinary_enabled(self) -> bool:
        """Return ``True`` only when Cloudinary credentials are present."""
        return bool(
            self.CLOUDINARY_CLOUD_NAME
            and self.CLOUDINARY_API_KEY
            and self.CLOUDINARY_API_SECRET
        )


@lru_cache
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance (dependency-injectable)."""
    return Settings()  # type: ignore[call-arg]


# Module-level singleton for convenient imports.
settings: Settings = get_settings()
