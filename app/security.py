import hashlib
import hmac
import secrets
import time

from fastapi import Header, HTTPException, status

from app.config import settings


def generate_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(24)


def sign_payload(secret: str, timestamp: int, body: bytes) -> str:
    """HMAC-SHA256 over "<timestamp>.<body>". The timestamp stops replay attacks."""
    message = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def build_signature_header(secret: str, timestamp: int, body: bytes) -> str:
    return f"t={timestamp},v1={sign_payload(secret, timestamp, body)}"


def verify_signature(secret: str, header: str, body: bytes, tolerance: int = 300, now: int | None = None) -> bool:
    """What a customer runs on their side to trust a webhook."""
    try:
        parts = dict(item.split("=", 1) for item in header.split(","))
        timestamp = int(parts["t"])
        received = parts["v1"]
    except (KeyError, ValueError):
        return False
    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > tolerance:
        return False
    expected = sign_payload(secret, timestamp, body)
    return hmac.compare_digest(expected, received)


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if not x_api_key or not hmac.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing X-API-Key")
