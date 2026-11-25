"""Run deliveries locally without Redis or Celery.

This is only a development convenience. Docker deployments continue to use the
Celery worker and beat services.
"""
import time

from app.database import SessionLocal
from app.delivery import attempt_delivery, find_due_delivery_ids


def run(interval_seconds: float = 10.0) -> None:
    print("HookRelay local worker started", flush=True)
    while True:
        with SessionLocal() as db:
            delivery_ids = find_due_delivery_ids(db)
            for delivery_id in delivery_ids:
                outcome = attempt_delivery(db, delivery_id)
                print(f"delivery {delivery_id}: {outcome}", flush=True)
        time.sleep(interval_seconds)


if __name__ == "__main__":
    run()
