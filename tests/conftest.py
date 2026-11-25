import os

# Must be set before the app is imported.
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["API_KEY"] = "test-key"
os.environ["CACHE_ENABLED"] = "false"
os.environ["MAX_ATTEMPTS"] = "3"
os.environ["BACKOFF_BASE_SECONDS"] = "10"

import pytest
from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app


class FakeTask:
    """Stands in for Celery so tests do not need Redis."""
    def __init__(self):
        self.queued = []

    def delay(self, delivery_id):
        self.queued.append(delivery_id)


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session


@pytest.fixture
def fake_task(monkeypatch):
    task = FakeTask()
    monkeypatch.setattr("app.main.deliver_task", task)
    return task


@pytest.fixture
def client(fake_task):
    return TestClient(app, headers={"X-API-Key": "test-key"})
