from celery import Celery

from app.config import settings
from app.database import SessionLocal
from app.delivery import attempt_delivery, find_due_delivery_ids

celery_app = Celery("hookrelay", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_acks_late=True,              # if a worker dies mid-task, the message is not lost
    worker_prefetch_multiplier=1,     # one job at a time, fair sharing between workers
    timezone="UTC",
    beat_schedule={
        "sweep-due-deliveries": {
            "task": "hookrelay.sweep",
            "schedule": float(settings.sweep_interval_seconds),
        }
    },
)


@celery_app.task(name="hookrelay.deliver")
def deliver_task(delivery_id: int) -> str:
    with SessionLocal() as db:
        return attempt_delivery(db, delivery_id)


@celery_app.task(name="hookrelay.sweep")
def sweep_task() -> int:
    """Safety net. Finds retries that are due and jobs lost by crashed workers or a down Redis."""
    with SessionLocal() as db:
        ids = find_due_delivery_ids(db)
    for delivery_id in ids:
        deliver_task.delay(delivery_id)
    return len(ids)
