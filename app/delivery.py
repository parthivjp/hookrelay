"""Core delivery logic. No Celery in here, so it is easy to test."""
import json
import random
import time
from datetime import timedelta

import httpx
from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Delivery, Endpoint, Event, utcnow
from app.security import build_signature_header


def compute_backoff(attempts_done: int) -> float:
    """Seconds to wait before the next try: 10, 20, 40, 80 ... capped, plus 10% jitter.

    Jitter stops a thousand failed deliveries from all retrying at the same second.
    """
    delay = min(settings.backoff_base_seconds * (2 ** (attempts_done - 1)), settings.backoff_max_seconds)
    return delay + random.uniform(0, delay * 0.1)


def _due_filter(now):
    return or_(
        Delivery.status == "pending",
        # A worker took it but never finished (crashed). Its lease ran out, so it is up for grabs.
        and_(Delivery.status == "in_progress", Delivery.locked_until < now),
    )


def find_due_delivery_ids(db: Session, limit: int = 500) -> list[int]:
    now = utcnow()
    stmt = (
        select(Delivery.id)
        .where(Delivery.next_attempt_at <= now, _due_filter(now))
        .order_by(Delivery.next_attempt_at)
        .limit(limit)
    )
    return list(db.scalars(stmt))


def claim_delivery(db: Session, delivery_id: int) -> Delivery | None:
    """Atomically take ownership of a delivery.

    One single UPDATE decides the winner. If two workers get the same job at
    the same time, the database lets only one of them change the row, so the
    customer never receives the same attempt twice.
    """
    now = utcnow()
    result = db.execute(
        update(Delivery)
        .where(Delivery.id == delivery_id, Delivery.next_attempt_at <= now, _due_filter(now))
        .values(status="in_progress", locked_until=now + timedelta(seconds=settings.lease_seconds))
        .execution_options(synchronize_session=False)  # the database decides, not Python
    )
    db.commit()
    if result.rowcount != 1:
        return None
    return db.get(Delivery, delivery_id, populate_existing=True)


def build_body(event: Event) -> bytes:
    return json.dumps(
        {
            "id": event.id,
            "type": event.event_type,
            "created_at": event.created_at.isoformat(),
            "data": event.payload,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def attempt_delivery(db: Session, delivery_id: int) -> str:
    """Try to deliver once. Returns: skipped | success | retry | dead."""
    delivery = claim_delivery(db, delivery_id)
    if delivery is None:
        return "skipped"  # someone else has it, or it is not due yet

    event = db.get(Event, delivery.event_id)
    endpoint = db.get(Endpoint, delivery.endpoint_id)

    if endpoint is None:
        delivery.status = "dead"
        delivery.last_error = "Endpoint not found"
        delivery.locked_until = None
        db.commit()
        return "dead"

    if not endpoint.active:
        delivery.status = "dead"
        delivery.last_error = "Endpoint is disabled"
        delivery.locked_until = None
        db.commit()
        return "dead"

    body = build_body(event)
    timestamp = int(time.time())
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "HookRelay/1.0",
        "X-HookRelay-Event-Id": str(event.id),
        "X-HookRelay-Delivery-Id": str(delivery.id),
        "X-HookRelay-Attempt": str(delivery.attempts + 1),
        "X-HookRelay-Signature": build_signature_header(endpoint.secret, timestamp, body),
    }

    status_code, error = None, None
    try:
        response = httpx.post(
            endpoint.url, content=body, headers=headers, timeout=settings.request_timeout_seconds
        )
        status_code = response.status_code
        if not 200 <= status_code < 300:
            error = f"HTTP {status_code}"
    except httpx.HTTPError as exc:
        error = f"{type(exc).__name__}: {exc}"[:500]

    delivery.attempts += 1
    delivery.last_status_code = status_code
    delivery.last_error = error
    delivery.locked_until = None

    if error is None:
        delivery.status = "success"
        delivery.delivered_at = utcnow()
        outcome = "success"
    elif delivery.attempts >= settings.max_attempts:
        delivery.status = "dead"  # dead-letter: stop trying, wait for a human to replay
        outcome = "dead"
    else:
        delivery.status = "pending"
        delivery.next_attempt_at = utcnow() + timedelta(seconds=compute_backoff(delivery.attempts))
        outcome = "retry"

    db.commit()
    return outcome
