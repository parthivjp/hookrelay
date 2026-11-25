from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class EndpointCreate(BaseModel):
    url: HttpUrl
    description: str | None = Field(default=None, max_length=255)
    event_types: list[str] = Field(default_factory=lambda: ["*"])
    secret: str | None = Field(default=None, min_length=8, max_length=128)  # optional, we generate one if empty


class EndpointUpdate(BaseModel):
    url: HttpUrl | None = None
    description: str | None = Field(default=None, max_length=255)
    event_types: list[str] | None = None
    active: bool | None = None


class EndpointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    url: str
    description: str | None
    event_types: list[str]
    active: bool
    created_at: datetime


class EndpointWithSecret(EndpointOut):
    secret: str  # shown only when created or rotated


class EventCreate(BaseModel):
    event_type: str = Field(min_length=1, max_length=100, examples=["order.created"])
    payload: dict


class DeliveryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    event_id: int
    endpoint_id: int
    status: str
    attempts: int
    next_attempt_at: datetime
    last_status_code: int | None
    last_error: str | None
    delivered_at: datetime | None
    created_at: datetime


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    event_type: str
    payload: dict
    idempotency_key: str | None
    created_at: datetime


class EventCreated(EventOut):
    duplicate: bool = False
    deliveries_created: int = 0


class EventDetail(EventOut):
    deliveries: list[DeliveryOut]
