import uuid
import json
import tempfile
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, File, Form, UploadFile, HTTPException

from app.api.schemas import (
    BuildingMetadata,
    JobStatus,
    JobSubmitResponse,
    JobStatusResponse,
)
from app.core.config import settings

import redis

router = APIRouter(tags=["Jobs"])

# Redis client - used to store and retrieve job state
redis_client = redis.Redis(
    host=settings.REDIS_HOST,
    port=settings.REDIS_PORT,
    db=0,
    decode_responses=True,
)

# Temp directory for uploaded PDFs
UPLOAD_DIR = Path(tempfile.gettempdir()) / "pdf_twin_uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# POST /jobs
# ---------------------------------------------------------------------------
@router.post("/jobs", response_model=JobSubmitResponse, summary="Submit a floor-plan PDF for conversion")
async def submit_job(
    file:          UploadFile = File(...,  description="Floor-plan PDF file"),
    building_name: str        = Form(...,  description="Name of the building"),
    address:       str        = Form(None, description="Physical address (optional)"),
    floors:        int        = Form(None, description="Expected number of floors (optional)"),
    scale_meters:  float      = Form(None, description="Known scale in metres per unit (optional)"),
):
    # Validate file type
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted.")

    # Read file bytes
    pdf_bytes = await file.read()
    if len(pdf_bytes) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    # Generate unique job ID
    job_id = str(uuid.uuid4())

    # ✅ Save PDF to /tmp/ - only pass file PATH to Celery, not bytes
    pdf_path = UPLOAD_DIR / f"{job_id}.pdf"
    pdf_path.write_bytes(pdf_bytes)

    # Build metadata dict
    metadata = BuildingMetadata(
        building_name=building_name,
        address=address,
        floors=floors,
        scale_meters=scale_meters,
    ).model_dump()

    # Persist initial job state in Redis
    job_record = {
        "job_id":     job_id,
        "status":     JobStatus.PENDING.value,
        "progress":   0,
        "result_url": None,
        "metadata":   None,
        "error":      None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    redis_client.setex(
        f"job:{job_id}",
        86400,  # expire after 24 hours
        json.dumps(job_record),
    )

    # ✅ Enqueue Celery task with file PATH only - not raw bytes
    from app.worker.tasks import process_pdf_job
    process_pdf_job.delay(job_id, str(pdf_path), metadata)

    return JobSubmitResponse(
        job_id=job_id,
        status=JobStatus.PENDING,
        message="Job accepted. Processing started in background.",
    )


# ---------------------------------------------------------------------------
# GET /jobs/{job_id}
# ---------------------------------------------------------------------------
@router.get("/jobs/{job_id}", response_model=JobStatusResponse, summary="Get job status and result")
async def get_job_status(job_id: str):
    raw = redis_client.get(f"job:{job_id}")
    if not raw:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    record = json.loads(raw)
    return JobStatusResponse(**record)