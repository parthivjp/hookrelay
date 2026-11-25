from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://hookrelay:hookrelay@localhost:5432/hookrelay"
    redis_url: str = "redis://localhost:6379/0"
    api_key: str = "dev-key"

    # Delivery rules
    max_attempts: int = 6              # after this many tries, the delivery goes to the dead-letter list
    backoff_base_seconds: int = 10     # wait 10s, 20s, 40s, 80s ...
    backoff_max_seconds: int = 3600    # never wait more than 1 hour
    request_timeout_seconds: int = 5   # slow customer servers must not block our workers
    lease_seconds: int = 60            # how long a worker "owns" a delivery
    sweep_interval_seconds: int = 10   # how often the sweeper looks for due deliveries

    cache_enabled: bool = True


settings = Settings()
