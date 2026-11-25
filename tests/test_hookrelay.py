import json
from datetime import timedelta

import httpx

from app.config import settings
from app.delivery import attempt_delivery, claim_delivery, compute_backoff, find_due_delivery_ids
from app.models import Delivery, utcnow
from app.security import build_signature_header, verify_signature


# ---------- helpers ----------

def make_endpoint(client, url="http://customer.test/hook", event_types=None, **extra):
    body = {"url": url, "event_types": event_types or ["*"], **extra}
    response = client.post("/endpoints", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def make_due(db, delivery_id):
    """Pretend the retry wait is over."""
    delivery = db.get(Delivery, delivery_id, populate_existing=True)
    delivery.next_attempt_at = utcnow() - timedelta(seconds=1)
    db.commit()


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


def patch_http(monkeypatch, status_code=200, error=None, calls=None):
    def fake_post(url, content=None, headers=None, timeout=None):
        if calls is not None:
            calls.append({"url": url, "body": content, "headers": headers})
        if error:
            raise error
        return FakeResponse(status_code)
    monkeypatch.setattr("app.delivery.httpx.post", fake_post)


# ---------- signatures ----------

def test_signature_roundtrip_and_tamper():
    body = b'{"hello":"world"}'
    header = build_signature_header("secret123", 1_000, body)
    assert verify_signature("secret123", header, body, now=1_010)
    assert not verify_signature("wrong-secret", header, body, now=1_010)
    assert not verify_signature("secret123", header, b'{"hello":"evil"}', now=1_010)


def test_old_signature_is_rejected_to_stop_replay_attacks():
    body = b"{}"
    header = build_signature_header("secret123", 1_000, body)
    assert not verify_signature("secret123", header, body, tolerance=300, now=1_000 + 301)


def test_garbage_signature_header_is_rejected():
    assert not verify_signature("s", "nonsense", b"{}")


# ---------- backoff ----------

def test_backoff_doubles_and_is_capped():
    first, second, third = (compute_backoff(n) for n in (1, 2, 3))
    assert 10 <= first <= 11
    assert 20 <= second <= 22
    assert 40 <= third <= 44
    assert compute_backoff(30) <= settings.backoff_max_seconds * 1.1


# ---------- API ----------

def test_api_key_is_required(client):
    assert client.get("/health").status_code == 200
    assert client.get("/endpoints", headers={"X-API-Key": "wrong"}).status_code == 401


def test_endpoint_crud(client):
    created = make_endpoint(client, description="orders")
    assert created["secret"].startswith("whsec_")

    fetched = client.get(f"/endpoints/{created['id']}").json()
    assert "secret" not in fetched  # secrets are shown only once

    updated = client.patch(f"/endpoints/{created['id']}", json={"active": False}).json()
    assert updated["active"] is False

    rotated = client.post(f"/endpoints/{created['id']}/rotate-secret").json()
    assert rotated["secret"] != created["secret"]

    assert client.delete(f"/endpoints/{created['id']}").status_code == 204
    assert client.get(f"/endpoints/{created['id']}").status_code == 404


def test_event_goes_only_to_matching_active_endpoints(client, fake_task):
    make_endpoint(client, event_types=["order.created"])
    make_endpoint(client, event_types=["order.*"])
    make_endpoint(client, event_types=["user.signup"])
    off = make_endpoint(client, event_types=["*"])
    client.patch(f"/endpoints/{off['id']}", json={"active": False})

    response = client.post("/events", json={"event_type": "order.created", "payload": {"id": 1}})
    assert response.status_code == 202
    assert response.json()["deliveries_created"] == 2
    assert len(fake_task.queued) == 2


def test_idempotency_key_prevents_duplicate_events(client, fake_task):
    make_endpoint(client)
    body = {"event_type": "order.created", "payload": {"id": 7}}
    headers = {"Idempotency-Key": "abc-123"}

    first = client.post("/events", json=body, headers=headers)
    second = client.post("/events", json=body, headers=headers)

    assert first.status_code == 202 and second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["id"] == first.json()["id"]
    assert len(fake_task.queued) == 1  # only one delivery was queued
    assert len(client.get("/events").json()) == 1


def test_same_idempotency_key_with_different_body_is_a_conflict(client):
    headers = {"Idempotency-Key": "k1"}
    client.post("/events", json={"event_type": "a", "payload": {"x": 1}}, headers=headers)
    response = client.post("/events", json={"event_type": "a", "payload": {"x": 2}}, headers=headers)
    assert response.status_code == 409


def test_event_is_saved_even_if_the_queue_is_down(client, monkeypatch):
    class BrokenTask:
        def delay(self, _):
            raise ConnectionError("redis is down")

    monkeypatch.setattr("app.main.deliver_task", BrokenTask())
    make_endpoint(client)
    response = client.post("/events", json={"event_type": "x", "payload": {}})
    assert response.status_code == 202  # not lost, the sweeper will pick it up


# ---------- delivery ----------

def test_successful_delivery_is_signed_and_marked_success(client, db, monkeypatch):
    endpoint = make_endpoint(client)
    event = client.post("/events", json={"event_type": "order.created", "payload": {"id": 1}}).json()
    delivery = db.query(Delivery).one()

    calls = []
    patch_http(monkeypatch, 200, calls=calls)
    assert attempt_delivery(db, delivery.id) == "success"

    sent = calls[0]
    assert verify_signature(endpoint["secret"], sent["headers"]["X-HookRelay-Signature"], sent["body"])
    assert json.loads(sent["body"])["data"] == {"id": 1}
    assert sent["headers"]["X-HookRelay-Event-Id"] == str(event["id"])

    db.expire_all()
    done = db.get(Delivery, delivery.id)
    assert done.status == "success" and done.attempts == 1 and done.delivered_at is not None


def test_failure_schedules_retry_with_backoff(client, db, monkeypatch):
    make_endpoint(client)
    client.post("/events", json={"event_type": "x", "payload": {}})
    delivery = db.query(Delivery).one()

    patch_http(monkeypatch, 500)
    assert attempt_delivery(db, delivery.id) == "retry"

    db.expire_all()
    row = db.get(Delivery, delivery.id)
    assert row.status == "pending" and row.attempts == 1
    assert row.last_error == "HTTP 500"
    # The retry is in the future, so it is not due yet and the sweeper ignores it.
    assert find_due_delivery_ids(db) == []
    assert attempt_delivery(db, delivery.id) == "skipped"


def test_network_error_is_treated_as_failure(client, db, monkeypatch):
    make_endpoint(client)
    client.post("/events", json={"event_type": "x", "payload": {}})
    delivery = db.query(Delivery).one()

    patch_http(monkeypatch, error=httpx.ConnectTimeout("too slow"))
    assert attempt_delivery(db, delivery.id) == "retry"
    db.expire_all()
    assert "ConnectTimeout" in db.get(Delivery, delivery.id).last_error


def test_dead_letter_after_max_attempts_then_replay(client, db, monkeypatch, fake_task):
    make_endpoint(client)
    client.post("/events", json={"event_type": "x", "payload": {}})
    delivery_id = db.query(Delivery).one().id

    patch_http(monkeypatch, 500)
    outcomes = []
    for _ in range(settings.max_attempts):
        make_due(db, delivery_id)
        outcomes.append(attempt_delivery(db, delivery_id))
    assert outcomes == ["retry", "retry", "dead"]

    dead = client.get("/deliveries", params={"status": "dead"}).json()
    assert [d["id"] for d in dead] == [delivery_id]

    # The customer fixed their server. A human replays it.
    fake_task.queued.clear()
    replay = client.post(f"/deliveries/{delivery_id}/replay")
    assert replay.status_code == 202
    assert replay.json()["status"] == "pending" and replay.json()["attempts"] == 0
    assert fake_task.queued == [delivery_id]

    patch_http(monkeypatch, 200)
    assert attempt_delivery(db, delivery_id) == "success"


def test_only_one_worker_can_claim_a_delivery(client, db):
    make_endpoint(client)
    client.post("/events", json={"event_type": "x", "payload": {}})
    delivery_id = db.query(Delivery).one().id

    assert claim_delivery(db, delivery_id) is not None   # worker A wins
    assert claim_delivery(db, delivery_id) is None       # worker B is turned away


def test_crashed_worker_is_recovered_when_lease_expires(client, db, monkeypatch):
    make_endpoint(client)
    client.post("/events", json={"event_type": "x", "payload": {}})
    delivery_id = db.query(Delivery).one().id

    assert claim_delivery(db, delivery_id) is not None   # worker takes it... and "crashes"
    assert find_due_delivery_ids(db) == []               # lease still valid, nobody else touches it

    row = db.get(Delivery, delivery_id, populate_existing=True)
    row.locked_until = utcnow() - timedelta(seconds=1)   # lease expired
    db.commit()

    assert find_due_delivery_ids(db) == [delivery_id]
    patch_http(monkeypatch, 200)
    assert attempt_delivery(db, delivery_id) == "success"


def test_disabled_endpoint_sends_nothing(client, db, monkeypatch):
    endpoint = make_endpoint(client)
    client.post("/events", json={"event_type": "x", "payload": {}})
    client.patch(f"/endpoints/{endpoint['id']}", json={"active": False})
    delivery_id = db.query(Delivery).one().id

    calls = []
    patch_http(monkeypatch, 200, calls=calls)
    assert attempt_delivery(db, delivery_id) == "dead"
    assert calls == []


def test_stats(client, db, monkeypatch):
    make_endpoint(client)
    client.post("/events", json={"event_type": "x", "payload": {}})
    delivery_id = db.query(Delivery).one().id
    patch_http(monkeypatch, 200)
    attempt_delivery(db, delivery_id)

    stats = client.get("/stats").json()
    assert stats["deliveries"]["success"] == 1
    assert stats["success_rate_percent"] == 100.0
    assert stats["events"] == 1 and stats["endpoints"] == 1
