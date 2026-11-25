"""A fake customer server for testing HookRelay.

It checks the signature (this is the code a real customer would write) and
fails randomly, so you can watch retries, backoff and the dead-letter list work.
"""
import hashlib
import hmac
import os
import random
import time

from fastapi import FastAPI, HTTPException, Request

SECRET = os.getenv("WEBHOOK_SECRET", "")
FAIL_RATE = float(os.getenv("FAIL_RATE", "0.5"))

app = FastAPI(title="Demo webhook receiver")
received: list[dict] = []
seen_event_ids: set[str] = set()


def verify(secret: str, header: str, body: bytes, tolerance: int = 300) -> bool:
    try:
        parts = dict(item.split("=", 1) for item in header.split(","))
        timestamp, sent = int(parts["t"]), parts["v1"]
    except (KeyError, ValueError):
        return False
    if abs(time.time() - timestamp) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sent)


@app.post("/hook")
async def hook(request: Request):
    body = await request.body()
    if SECRET and not verify(SECRET, request.headers.get("X-HookRelay-Signature", ""), body):
        raise HTTPException(401, "Bad signature")

    event_id = request.headers.get("X-HookRelay-Event-Id", "?")
    attempt = request.headers.get("X-HookRelay-Attempt", "?")

    if random.random() < FAIL_RATE:
        print(f"[receiver] event {event_id} attempt {attempt} -> FAIL (500)", flush=True)
        raise HTTPException(500, "Random failure")

    # Webhooks are "at least once", so a good receiver ignores events it has already processed.
    duplicate = event_id in seen_event_ids
    seen_event_ids.add(event_id)
    received.append({"event_id": event_id, "attempt": attempt, "duplicate": duplicate})
    print(f"[receiver] event {event_id} attempt {attempt} -> OK", flush=True)
    return {"ok": True}


@app.get("/received")
def list_received():
    return received
