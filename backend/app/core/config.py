from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://ticketrush:ticketrush@localhost:5432/ticketrush"
    redis_url: str = "redis://localhost:6379/0"

    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"

    hold_ttl_seconds: int = 300
    checkout_payment_failure_rate: float = 0.0
    checkout_payment_latency_ms: int = 150

    waiting_room_batch_size: int = 200
    waiting_room_batch_interval_seconds: float = 2.0

    hold_strategy: str = "postgres"  # "postgres" | "redis"

    environment: str = "development"


@lru_cache
def get_settings() -> Settings:
    return Settings()
