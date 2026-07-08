import json
import os
from datetime import datetime, timezone
from pathlib import Path

import redis

from app.core.celery_app import celery_app
from app.core.config import settings

# Redis client for updating job state
redis_client = redis.Redis(
    host=settings.REDIS_HOST,
    port=settings.REDIS_PORT,
    db=0,
    decode_responses=True,
)


def _update_job(job_id: str, **kwargs):
    """Helper to update job state in Redis."""
    raw = redis_client.get(f"job:{job_id}")
    if not raw:
        return
    record = json.loads(raw)
    record.update(kwargs)
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    redis_client.setex(f"job:{job_id}", 86400, json.dumps(record))


@celery_app.task(bind=True, name="app.worker.tasks.process_pdf_job", max_retries=3)
def process_pdf_job(self, job_id: str, pdf_path: str, metadata: dict):
    """
    Main Celery task - orchestrates the full PDF → glTF pipeline.

    Steps:
        1. Extract  - PDF → page images + raw geometry
        2. Interpret - Claude Vision → structured floor plan schema
        3. Reconstruct - 2D schema → 3D mesh
        4. Emit      - 3D mesh → glTF/GLB + S3 upload
        5. Callback  - notify platform with result URL
    """
    try:
        # ---------------------------------------------------------------
        # STEP 1: Mark job as processing
        # ---------------------------------------------------------------
        _update_job(job_id, status="processing", progress=5)

        # ---------------------------------------------------------------
        # STEP 2: Extract - PDF → images + geometry
        # ---------------------------------------------------------------
        from app.services.extractor import extract_pdf
        _update_job(job_id, progress=15)
        extraction = extract_pdf(pdf_path, metadata)
        # extraction = {
        #   "pages": [{"image_b64": "...", "page_index": 0}, ...],
        #   "vector_geometry": [...] or None
        # }

        # ---------------------------------------------------------------
        # STEP 3: Interpret - Claude Vision → floor plan schema
        # ---------------------------------------------------------------
        from app.services.interpreter import interpret_floor_plan
        _update_job(job_id, progress=40)
        floor_schema = interpret_floor_plan(extraction, metadata)
        # floor_schema = {
        #   "floors": [{"index": 0, "rooms": [...], "walls": [...], "height_m": 3.0}],
        #   "confidence": 0.92
        # }

        # ---------------------------------------------------------------
        # STEP 4: Reconstruct - 2D schema → 3D mesh
        # ---------------------------------------------------------------
        from app.services.reconstructor import reconstruct_3d
        _update_job(job_id, progress=65)
        mesh_path = reconstruct_3d(job_id, floor_schema)
        # mesh_path = /tmp/pdf_twin_outputs/{job_id}/model.glb

        # ---------------------------------------------------------------
        # STEP 5: Emit - upload glTF to S3
        # ---------------------------------------------------------------
        from app.services.emitter import emit_artifact
        _update_job(job_id, progress=85)
        result_url = emit_artifact(job_id, mesh_path, floor_schema)
        # result_url = https://s3.amazonaws.com/bucket/{job_id}/model.glb

        # ---------------------------------------------------------------
        # STEP 6: Complete - update job state
        # ---------------------------------------------------------------
        _update_job(
            job_id,
            status="completed",
            progress=100,
            result_url=result_url,
            metadata={
                "floors":     len(floor_schema.get("floors", [])),
                "confidence": floor_schema.get("confidence", 0),
                "building":   metadata.get("building_name"),
            },
        )

        # ---------------------------------------------------------------
        # STEP 7: Callback - notify platform
        # ---------------------------------------------------------------
        if settings.PLATFORM_CALLBACK_URL:
            _send_callback(job_id, "completed", result_url, floor_schema)

        # Cleanup temp PDF
        Path(pdf_path).unlink(missing_ok=True)

    except Exception as exc:
        # Mark job as failed
        _update_job(job_id, status="failed", error=str(exc))

        # Notify platform of failure
        if settings.PLATFORM_CALLBACK_URL:
            _send_callback(job_id, "failed", None, None, error=str(exc))

        # Retry up to max_retries with exponential backoff
        raise self.retry(exc=exc, countdown=2 ** self.request.retries)


def _send_callback(job_id: str, status: str, result_url, floor_schema, error=None):
    """POST job result to the main platform webhook."""
    import requests
    payload = {
        "job_id":     job_id,
        "status":     status,
        "result_url": result_url,
        "confidence": floor_schema.get("confidence") if floor_schema else None,
        "error":      error,
    }
    try:
        requests.post(settings.PLATFORM_CALLBACK_URL, json=payload, timeout=10)
    except Exception:
        pass  # Callback failure should not affect job state