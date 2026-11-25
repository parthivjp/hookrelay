import json

import redis

from app.config import settings

_client = None


def _redis():
    global _client
    if _client is None:
        _client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1)
    return _client


def get_json(key: str):
    if not settings.cache_enabled:
        return None
    try:
        raw = _redis().get(key)
        return json.loads(raw) if raw else None
    except redis.RedisError:
        return None  # cache is optional, never break the API because of it


def set_json(key: str, value, ttl: int = 10) -> None:
    if not settings.cache_enabled:
        return
    try:
        _redis().setex(key, ttl, json.dumps(value))
    except redis.RedisError:
        pass
