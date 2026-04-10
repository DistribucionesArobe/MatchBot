"""
MatchBot - Configuration
https://matchbot.live
"""
import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    # -- Database --
    DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql://matchbot:matchbot@localhost:5432/matchbot")

    # -- WhatsApp (Meta Cloud API) --
    WA_VERIFY_TOKEN: str = os.getenv("WA_VERIFY_TOKEN", "matchbot-verify-token")
    # Per-club tokens are stored in the clubs table, but we can have a default
    WA_API_VERSION: str = "v21.0"
    WA_API_BASE: str = "https://graph.facebook.com"

    # -- App --
    APP_NAME: str = "MatchBot"
    APP_URL: str = os.getenv("APP_URL", "https://matchbot.live")
    SECRET_KEY: str = os.getenv("SECRET_KEY", "change-me-in-production")
    DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"

    # -- Booking defaults --
    DEFAULT_SLOT_MINUTES: int = 90
    BOOKING_EXPIRY_MINUTES: int = 15        # tiempo para pagar antes de liberar
    MAX_ADVANCE_DAYS: int = 7               # reservar hasta 7 días adelante
    CANCELLATION_HOURS: int = 4             # cancelar gratis hasta 4h antes

    # -- Message buffering --
    MSG_BUFFER_SECONDS: float = 5.0         # esperar mensajes rápidos

    # -- Stripe (optional) --
    STRIPE_SECRET_KEY: str = os.getenv("STRIPE_SECRET_KEY", "")
    STRIPE_WEBHOOK_SECRET: str = os.getenv("STRIPE_WEBHOOK_SECRET", "")

    @property
    def wa_api_url(self) -> str:
        return f"{self.WA_API_BASE}/{self.WA_API_VERSION}"


settings = Settings()
