import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    api_token: str
    timezone: ZoneInfo
    sync_rate_limit: int = 120
    mcp_rate_limit: int = 60
    auth_fail_limit: int = 30
    max_body_bytes: int = 4_000_000
    max_push_records: int = 5000
    purge_interval_seconds: int = 6 * 3600


def load_settings() -> Settings:
    token = os.environ.get("API_TOKEN", "")
    if len(token) < 32:
        # Never echo the value itself.
        raise RuntimeError("API_TOKEN must be set and at least 32 characters (use `openssl rand -hex 32`)")
    return Settings(
        database_url=os.environ["DATABASE_URL"],
        redis_url=os.environ.get("REDIS_URL", "redis://redis:6379/0"),
        api_token=token,
        timezone=ZoneInfo(os.environ.get("APP_TIMEZONE", "Africa/Kampala")),
    )
