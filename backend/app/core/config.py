from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Phase 1 settings. Later phases extend this class rather than replace it."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str

    @property
    def sync_database_url(self) -> str:
        """Driver swap for the Celery worker, which runs synchronously."""
        return self.DATABASE_URL.replace("+asyncpg", "+psycopg")


@lru_cache
def get_settings() -> Settings:
    return Settings()
