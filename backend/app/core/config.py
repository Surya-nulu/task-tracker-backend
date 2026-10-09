# from functools import lru_cache

# from pydantic_settings import BaseSettings, SettingsConfigDict


# class Settings(BaseSettings):
#     """Phase 1 settings. Later phases extend this class rather than replace it."""

#     model_config = SettingsConfigDict(env_file=".env", extra="ignore")

#     DATABASE_URL: str

#     @property
#     def sync_database_url(self) -> str:
#         """Driver swap for the Celery worker, which runs synchronously."""
#         return self.DATABASE_URL.replace("+asyncpg", "+psycopg")


# @lru_cache
# def get_settings() -> Settings:
#     return Settings()


from functools import lru_cache
from typing import List, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Single source of truth for configuration. Later phases add fields, never replace this."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Database --------------------------------------------------------
    DATABASE_URL: str

    # --- Auth (consumed from Phase 3) ------------------------------------
    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    # --- Redis / Celery (consumed from Phase 9) --------------------------
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: Optional[str] = None

    # --- Application -----------------------------------------------------
    API_PREFIX: str = "/api"
    ENVIRONMENT: str = "development"
    CORS_ORIGINS: List[str] = ["http://localhost:5173"]

    # --- Alarm worker (consumed from Phase 10) ---------------------------
    ALARM_CHECK_INTERVAL_SECONDS: int = 60
    ALARM_BATCH_SIZE: int = 100

    @property
    def sync_database_url(self) -> str:
        """Driver swap for the Celery worker, which runs synchronously."""
        return self.DATABASE_URL.replace("+asyncpg", "+psycopg")

    @property
    def celery_broker(self) -> str:
        """Falls back to REDIS_URL so dev only needs one Redis variable."""
        return self.CELERY_BROKER_URL or self.REDIS_URL

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
