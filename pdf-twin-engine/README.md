# PDF to Twin Conversion Engine

An SQS-consumer external processor that converts 2D architectural floor plan PDFs into 3D Digital Twin models in glTF/GLB format, running inside the MWX-X2 platform's compose stack and feeding the Cesium viewer.

---

## Overview

The engine is an X2 "external processor" for the `pdf_twin` job kind (see
`x2-backend/worker/CONTRACT.md`). It consumes job dispatch from a dedicated
SQS-compatible queue (`SQS_QUEUE_PDF_TWIN`, default `x2-jobs-pdf_twin`),
downloads the source PDF from the shared uploads bucket, runs it through a
multi-stage AI pipeline, and writes the result directly to the shared
Postgres `processing_jobs`/`artifacts` tables and the shared artifacts
bucket — the same contract every other X2 job kind (Go or Python) writes.

```
SQS dispatch → download PDF → Extract → Claude Vision Interpret → 3D Reconstruct → GLB upload + artifact row
```

## Architecture

```
pdf-twin-engine/
├── app/
│   ├── core/
│   │   ├── config.py        # Environment config via pydantic-settings
│   │   └── db.py            # Postgres access for the job/artifact contract
│   ├── services/
│   │   ├── extractor.py     # PyMuPDF — PDF → base64 images + vector geometry
│   │   ├── interpreter.py   # Claude Vision — images → structured floor schema
│   │   ├── reconstructor.py # trimesh — 2D schema → 3D GLB mesh
│   │   └── emitter.py       # boto3 — GLB upload to the shared artifacts bucket
│   └── worker/
│       └── consumer.py      # SQS long-poll loop: claim, heartbeat, run pipeline, persist
├── Dockerfile
├── requirements.txt
└── .env.example
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|--------------|
| `ANTHROPIC_API_KEY` | ✅ | Claude Vision API key (same value X2's `internal/ai` module is provisioned with) |
| `DB_HOST`/`DB_PORT`/`DB_USER`/`DB_PASSWORD`/`DB_NAME` | ✅ | Shared Postgres |
| `S3_ENDPOINT`/`S3_REGION`/`S3_ACCESS_KEY`/`S3_SECRET_KEY`/`S3_BUCKET_ARTIFACTS` | ✅ | Shared S3-compatible artifact storage |
| `SQS_ENDPOINT`/`SQS_REGION`/`SQS_QUEUE_PDF_TWIN` | ✅ | Shared SQS-compatible dispatch queue |

## Running it

This engine is one service inside the X2 compose stack
(`x2-backend/deploy/docker-compose.yml`, service `pdf-twin-engine`) — it is
not run standalone. `docker compose up -d --build pdf-twin-engine` from
`x2-backend/deploy` builds and starts it alongside Postgres/MinIO/ElasticMQ.

---

## Integration with MWX-X2 Platform

The engine is called through the shared X2 job contract, not directly:

1. `x2-backend` creates a `processing_jobs` row for a `pdf_twin` job and dispatches a message to `SQS_QUEUE_PDF_TWIN`.
2. This consumer claims the message, downloads the source PDF from the shared uploads bucket, and runs the pipeline.
3. It periodically heartbeats the job row while running, and marks it `completed`/`failed` on exit.
4. On success, it uploads the GLB to the shared artifacts bucket and writes an `artifacts` row pointing to it.
5. `x2-backend`/the portals read job status and artifact rows straight from Postgres — there is no callback or webhook.

The GLB output is compatible with the Cesium viewer used in the MWX-X2 Digital Twin dashboard.

---

## Tech Stack

- **boto3** — SQS job dispatch consumption and S3-compatible artifact storage
- **psycopg** — Shared Postgres access for the job/artifact contract
- **PyMuPDF** — PDF rendering and vector extraction
- **Anthropic Claude Vision** — AI floor plan interpretation
- **trimesh + Shapely** — 3D mesh reconstruction
- **pygltflib** — glTF/GLB export
- **Docker** — Containerised deployment