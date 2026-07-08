from celery import Celery
from app.core.config import settings

# Initialize Celery with Redis as both broker and result backend
celery_app = Celery(
    "pdf_twin_worker",
    broker=f"redis://{settings.REDIS_HOST}:{settings.REDIS_PORT}/0",
    backend=f"redis://{settings.REDIS_HOST}:{settings.REDIS_PORT}/1",
    include=["app.worker.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,           # Task ack කරන්නේ complete වුනාට පස්සේ - crash safe
    worker_prefetch_multiplier=1,  # One task per worker at a time
    task_routes={
        "app.worker.tasks.process_pdf_job": {"queue": "pdf_processing"},
    },
)