# PDF to Twin Conversion Engine

A standalone FastAPI microservice that converts 2D architectural floor plan PDFs into 3D Digital Twin models in glTF/GLB format, ready for integration with the MWX-X2 platform and Cesium viewer.

---

## Overview

The engine accepts a floor plan PDF and building metadata via REST API, processes it through a multi-stage AI pipeline, and returns a downloadable 3D GLB model along with structured floor schema data.

```
PDF Upload → Extract → Claude Vision Interpret → 3D Reconstruct → GLB Export → Platform Callback
```

---

## Architecture

```
pdf-twin-engine/
├── app/
│   ├── api/
│   │   ├── routes.py        # POST /jobs, GET /jobs/{id}
│   │   └── schemas.py       # Pydantic request/response models
│   ├── core/
│   │   ├── config.py        # Environment config via pydantic-settings
│   │   └── celery_app.py    # Celery + Redis broker setup
│   ├── services/
│   │   ├── extractor.py     # PyMuPDF — PDF → base64 images + vector geometry
│   │   ├── interpreter.py   # Claude Vision — images → structured floor schema
│   │   ├── reconstructor.py # trimesh — 2D schema → 3D GLB mesh
│   │   └── emitter.py       # boto3 — GLB upload to S3 + platform callback
│   ├── worker/
│   │   └── tasks.py         # Celery task orchestrating the full pipeline
│   └── main.py              # FastAPI app initialisation
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

---

## Pipeline Stages

| Stage | Service | Description |
|-------|---------|-------------|
| 1. Intake | `routes.py` | Receives PDF + metadata, saves to `/tmp/`, enqueues Celery job |
| 2. Extract | `extractor.py` | Renders each PDF page to high-res JPEG; extracts vector geometry from CAD PDFs |
| 3. Interpret | `interpreter.py` | Sends page images to Claude Vision; returns structured JSON with floors, walls, rooms |
| 4. Reconstruct | `reconstructor.py` | Extrudes wall segments into 3D boxes; stacks floors with concrete slabs |
| 5. Emit | `emitter.py` | Uploads GLB to S3; fires webhook callback to main platform |

---

## API Reference

### Submit a job

```
POST /api/v1/jobs
Content-Type: multipart/form-data
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `file` | PDF | ✅ | Floor plan PDF |
| `building_name` | string | ✅ | Name of the building |
| `address` | string | — | Physical address |
| `floors` | integer | — | Expected number of floors |
| `scale_meters` | float | — | Known scale in metres per unit |

**Response**
```json
{
  "job_id": "b01847ba-ddbb-4378-8ce7-c88c06629f14",
  "status": "pending",
  "message": "Job accepted. Processing started in background."
}
```

---

### Check job status

```
GET /api/v1/jobs/{job_id}
```

**Response**
```json
{
  "job_id": "b01847ba-ddbb-4378-8ce7-c88c06629f14",
  "status": "completed",
  "progress": 100,
  "result_url": "https://s3.amazonaws.com/bucket/twins/b01847ba/model.glb",
  "metadata": {
    "floors": 2,
    "confidence": 0.94,
    "building": "Tower A"
  }
}
```

Job status values: `pending` → `processing` → `completed` / `failed`

---

### Health check

```
GET /health
```

```json
{ "status": "healthy", "service": "pdf-twin-engine" }
```

---

## Getting Started

### Prerequisites
- Docker Desktop
- Anthropic API key
- AWS S3 bucket (optional — dev mode uses local file path)

### Setup

```bash
git clone <repo>
cd pdf-twin-engine

cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY
```

### Run

```bash
docker-compose up --build
```

This starts three containers:
- `pdf_twin_redis` — message broker
- `pdf_twin_api` — REST API on port 8000
- `pdf_twin_worker` — Celery background processor

### Test

Open the interactive API docs:
```
http://localhost:8000/docs
```

Or submit a job via curl:
```bash
curl -X POST http://localhost:8000/api/v1/jobs \
  -F "file=@floorplan.pdf;type=application/pdf" \
  -F "building_name=Tower A" \
  -F "floors=3"
```

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | ✅ | Claude Vision API key |
| `REDIS_HOST` | ✅ | Redis host (default: `redis`) |
| `REDIS_PORT` | ✅ | Redis port (default: `6379`) |
| `AWS_ACCESS_KEY_ID` | — | S3 upload (optional in dev) |
| `AWS_SECRET_ACCESS_KEY` | — | S3 upload (optional in dev) |
| `AWS_S3_BUCKET` | — | S3 bucket name |
| `AWS_S3_REGION` | — | S3 region (default: `us-east-1`) |
| `PLATFORM_CALLBACK_URL` | — | Webhook URL for job completion |

---

## Integration with MWX-X2 Platform

The service is designed to be called as an independent entity from the main platform:

1. Platform POSTs a floor plan PDF to `POST /api/v1/jobs`
2. Service returns a `job_id` immediately
3. Platform polls `GET /api/v1/jobs/{job_id}` for status
4. On completion, the GLB model URL is available in `result_url`
5. Optionally, the service fires a webhook to `PLATFORM_CALLBACK_URL` with the result

The GLB output is compatible with the Cesium viewer used in the MWX-X2 Digital Twin dashboard.

---

## Tech Stack

- **FastAPI** — REST API framework
- **Celery + Redis** — Async job queue
- **PyMuPDF** — PDF rendering and vector extraction
- **Anthropic Claude Vision** — AI floor plan interpretation
- **trimesh + Shapely** — 3D mesh reconstruction
- **pygltflib** — glTF/GLB export
- **boto3** — AWS S3 artifact storage
- **Docker** — Containerised deployment