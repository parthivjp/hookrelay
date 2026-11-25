from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app import cache
from app.database import Base, engine, get_db
from app.models import Delivery, Endpoint, Event, utcnow
from app.schemas import (
    DeliveryOut, EndpointCreate, EndpointOut, EndpointUpdate, EndpointWithSecret,
    EventCreate, EventCreated, EventDetail, EventOut,
)
from app.security import generate_secret, require_api_key
from app.worker import deliver_task


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)  # simple for a small project; use Alembic for real migrations
    yield


app = FastAPI(
    title="HookRelay",
    description="Reliable webhook delivery: retries, signatures, dead-letter queue and replay.",
    version="1.0.0",
    lifespan=lifespan,
)
api = APIRouter(dependencies=[Depends(require_api_key)])


# ---------- helpers ----------

def event_type_matches(endpoint: Endpoint, event_type: str) -> bool:
    """'*' = everything, 'order.*' = prefix match, 'order.created' = exact match."""
    for pattern in endpoint.event_types:
        if pattern == "*" or pattern == event_type:
            return True
        if pattern.endswith(".*") and event_type.startswith(pattern[:-1]):
            return True
    return False


def enqueue(deliveries: list[Delivery]) -> None:
    for delivery in deliveries:
        try:
            deliver_task.delay(delivery.id)
        except Exception:
            # Redis/broker is down. Not a problem: the rows are already saved in PostgreSQL
            # and the sweeper will pick them up when the broker is back.
            break


def get_endpoint_or_404(db: Session, endpoint_id: int) -> Endpoint:
    endpoint = db.get(Endpoint, endpoint_id)
    if endpoint is None:
        raise HTTPException(404, "Endpoint not found")
    return endpoint


# ---------- health ----------

@app.get("/health", tags=["system"])
def health():
    return {"status": "ok"}


# ---------- endpoints (CRUD) ----------

@api.post("/endpoints", response_model=EndpointWithSecret, status_code=201, tags=["endpoints"])
def create_endpoint(body: EndpointCreate, db: Session = Depends(get_db)):
    if not body.event_types:
        raise HTTPException(422, "event_types cannot be empty. Use ['*'] for everything.")
    endpoint = Endpoint(
        url=str(body.url),
        description=body.description,
        event_types=body.event_types,
        secret=body.secret or generate_secret(),
    )
    db.add(endpoint)
    db.commit()
    return endpoint


@api.get("/endpoints", response_model=list[EndpointOut], tags=["endpoints"])
def list_endpoints(db: Session = Depends(get_db)):
    return db.scalars(select(Endpoint).order_by(Endpoint.id)).all()


@api.get("/endpoints/{endpoint_id}", response_model=EndpointOut, tags=["endpoints"])
def get_endpoint(endpoint_id: int, db: Session = Depends(get_db)):
    return get_endpoint_or_404(db, endpoint_id)


@api.patch("/endpoints/{endpoint_id}", response_model=EndpointOut, tags=["endpoints"])
def update_endpoint(endpoint_id: int, body: EndpointUpdate, db: Session = Depends(get_db)):
    endpoint = get_endpoint_or_404(db, endpoint_id)
    changes = body.model_dump(exclude_unset=True)
    if "url" in changes and changes["url"] is not None:
        changes["url"] = str(changes["url"])
    if changes.get("event_types") == []:
        raise HTTPException(422, "event_types cannot be empty. Use ['*'] for everything.")
    for field, value in changes.items():
        if value is not None:
            setattr(endpoint, field, value)
    db.commit()
    return endpoint


@api.post("/endpoints/{endpoint_id}/rotate-secret", response_model=EndpointWithSecret, tags=["endpoints"])
def rotate_secret(endpoint_id: int, db: Session = Depends(get_db)):
    endpoint = get_endpoint_or_404(db, endpoint_id)
    endpoint.secret = generate_secret()
    db.commit()
    return endpoint


@api.delete("/endpoints/{endpoint_id}", status_code=204, tags=["endpoints"])
def delete_endpoint(endpoint_id: int, db: Session = Depends(get_db)):
    db.delete(get_endpoint_or_404(db, endpoint_id))
    db.commit()
    return Response(status_code=204)


# ---------- events ----------

def _existing_event_response(existing: Event, body: EventCreate, response: Response) -> EventCreated:
    if existing.event_type != body.event_type or existing.payload != body.payload:
        raise HTTPException(409, "This Idempotency-Key was already used with a different request")
    response.status_code = 200  # same request again: return the old result, create nothing new
    return EventCreated(**EventOut.model_validate(existing).model_dump(), duplicate=True, deliveries_created=0)


@api.post("/events", response_model=EventCreated, status_code=202, tags=["events"])
def create_event(
    body: EventCreate,
    response: Response,
    idempotency_key: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    """Send an event. It is fanned out to every active endpoint that subscribes to its type.

    Send an `Idempotency-Key` header so that if your client retries this call,
    the event is not created (and delivered) twice.
    """
    if idempotency_key:
        existing = db.scalar(select(Event).where(Event.idempotency_key == idempotency_key))
        if existing:
            return _existing_event_response(existing, body, response)

    event = Event(event_type=body.event_type, payload=body.payload, idempotency_key=idempotency_key)
    db.add(event)
    try:
        db.flush()
    except IntegrityError:
        # Two identical requests arrived at the same moment. The unique index let only one in.
        db.rollback()
        existing = db.scalar(select(Event).where(Event.idempotency_key == idempotency_key))
        return _existing_event_response(existing, body, response)

    endpoints = db.scalars(select(Endpoint).where(Endpoint.active.is_(True))).all()
    deliveries = [
        Delivery(event_id=event.id, endpoint_id=endpoint.id)
        for endpoint in endpoints
        if event_type_matches(endpoint, event.event_type)
    ]
    db.add_all(deliveries)
    db.commit()

    enqueue(deliveries)
    return EventCreated(**EventOut.model_validate(event).model_dump(), deliveries_created=len(deliveries))


@api.get("/events", response_model=list[EventOut], tags=["events"])
def list_events(
    event_type: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    stmt = select(Event).order_by(Event.id.desc()).limit(limit).offset(offset)
    if event_type:
        stmt = stmt.where(Event.event_type == event_type)
    return db.scalars(stmt).all()


@api.get("/events/{event_id}", response_model=EventDetail, tags=["events"])
def get_event(event_id: int, db: Session = Depends(get_db)):
    event = db.scalar(select(Event).where(Event.id == event_id).options(selectinload(Event.deliveries)))
    if event is None:
        raise HTTPException(404, "Event not found")
    return event


# ---------- deliveries ----------

@api.get("/deliveries", response_model=list[DeliveryOut], tags=["deliveries"])
def list_deliveries(
    status_filter: str | None = Query(None, alias="status", pattern="^(pending|in_progress|success|dead)$"),
    endpoint_id: int | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Use ?status=dead to see the dead-letter list."""
    stmt = select(Delivery).order_by(Delivery.id.desc()).limit(limit).offset(offset)
    if status_filter:
        stmt = stmt.where(Delivery.status == status_filter)
    if endpoint_id:
        stmt = stmt.where(Delivery.endpoint_id == endpoint_id)
    return db.scalars(stmt).all()


@api.post("/deliveries/{delivery_id}/replay", response_model=DeliveryOut, status_code=202, tags=["deliveries"])
def replay_delivery(delivery_id: int, db: Session = Depends(get_db)):
    """Send a delivery again from scratch (for example after the customer fixed their server)."""
    delivery = db.get(Delivery, delivery_id)
    if delivery is None:
        raise HTTPException(404, "Delivery not found")
    if delivery.status == "in_progress":
        raise HTTPException(409, "Delivery is being processed right now")
    delivery.status = "pending"
    delivery.attempts = 0
    delivery.next_attempt_at = utcnow()
    delivery.locked_until = None
    delivery.last_error = None
    delivery.last_status_code = None
    delivery.delivered_at = None
    db.commit()
    enqueue([delivery])
    return delivery


# ---------- stats ----------

@api.get("/stats", tags=["system"])
def stats(db: Session = Depends(get_db)):
    """Counts for a dashboard. Cached in Redis for 10 seconds so heavy polling stays cheap."""
    cached = cache.get_json("hookrelay:stats")
    if cached is not None:
        return {**cached, "cached": True}

    by_status = dict(db.execute(select(Delivery.status, func.count()).group_by(Delivery.status)).all())
    success, dead = by_status.get("success", 0), by_status.get("dead", 0)
    finished = success + dead
    result = {
        "deliveries": {s: by_status.get(s, 0) for s in ("pending", "in_progress", "success", "dead")},
        "success_rate_percent": round(success / finished * 100, 2) if finished else None,
        "events": db.scalar(select(func.count(Event.id))),
        "endpoints": db.scalar(select(func.count(Endpoint.id))),
    }
    cache.set_json("hookrelay:stats", result, ttl=10)
    return {**result, "cached": False}


app.include_router(api)
