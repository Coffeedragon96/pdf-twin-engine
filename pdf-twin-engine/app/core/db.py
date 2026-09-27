"""Postgres access for the processing_jobs/artifacts contract this engine
implements as an external processor (see x2-backend/worker/CONTRACT.md).

The processing_jobs row is the source of record; this engine only ever
processes rows whose status is 'queued', and only for kind='pdf_twin'.
"""

import json
import socket
from datetime import datetime, timezone

import psycopg

from app.core.config import settings


def _now():
    return datetime.now(timezone.utc)


def _decode_jsonb(value):
    """psycopg3 returns jsonb as decoded Python objects; tolerate raw JSON
    text and NULL too."""
    if value is None:
        return {}
    if isinstance(value, (str, bytes, bytearray)):
        return json.loads(value)
    return value


class Database:
    def __init__(self):
        self._dsn = (
            f"host={settings.DB_HOST} port={settings.DB_PORT} "
            f"user={settings.DB_USER} password={settings.DB_PASSWORD} "
            f"dbname={settings.DB_NAME}"
        )

    def connect(self):
        return psycopg.connect(self._dsn, autocommit=True)

    def healthy(self) -> bool:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            return cur.fetchone() == (1,)

    def claim_job(self, job_uuid: str):
        """Atomically move a queued job to running; returns the job dict or
        None if it was already claimed, canceled, or never existed."""
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE processing_jobs
                   SET status = 'running', claimed_by = %s, claimed_at = %s,
                       heartbeat_at = %s, updated_at = %s
                 WHERE job_uuid = %s AND status = 'queued' AND deleted_at IS NULL
                RETURNING id, org_id, project_id, kind, input
                """,
                (socket.gethostname(), _now(), _now(), _now(), job_uuid),
            )
            row = cur.fetchone()
            if row is None:
                return None
            job = {
                "id": row[0],
                "job_uuid": job_uuid,
                "org_id": row[1],
                "project_id": row[2],
                "kind": row[3],
                "input": _decode_jsonb(row[4]),
            }
            cur.execute("SELECT org_uuid FROM organizations WHERE id = %s", (job["org_id"],))
            org = cur.fetchone()
            job["org_uuid"] = str(org[0]) if org else ""
            return job

    def heartbeat(self, job_id: int) -> None:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE processing_jobs SET heartbeat_at = %s, updated_at = %s WHERE id = %s",
                (_now(), _now(), job_id),
            )

    def progress(self, job_id: int, percent: int, stage: str) -> None:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE processing_jobs
                      SET progress = %s, stage = %s, heartbeat_at = %s, updated_at = %s
                    WHERE id = %s AND status = 'running'""",
                (percent, stage, _now(), _now(), job_id),
            )

    def fail(self, job: dict, message: str) -> None:
        with psycopg.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE processing_jobs
                          SET status = 'failed', error = %s, updated_at = %s
                        WHERE id = %s AND status = 'running'""",
                    (message[:4000], _now(), job["id"]),
                )
            conn.commit()

    def succeed_with_artifact(self, job: dict, artifact_uuid: str, name: str,
                               root_path: str, entry_file: str, size_bytes: int,
                               metadata: dict) -> None:
        """Mark the job succeeded and insert its 'glb' artifact in one
        transaction — the same contract every other job kind writes."""
        with psycopg.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE processing_jobs
                          SET status = 'succeeded', progress = 100, stage = 'done', updated_at = %s
                        WHERE id = %s AND status = 'running'""",
                    (_now(), job["id"]),
                )
                if cur.rowcount != 1:
                    raise RuntimeError("job left running state during processing (canceled?)")
                cur.execute(
                    """INSERT INTO artifacts
                           (created_at, updated_at, artifact_uuid, org_id, project_id, job_id,
                            kind, name, root_path, entry_file, size_bytes, metadata, status)
                       VALUES (%s, %s, %s, %s, %s, %s, 'glb', %s, %s, %s, %s, %s, 'ready')""",
                    (_now(), _now(), artifact_uuid, job["org_id"], job["project_id"],
                     job["id"], name, root_path, entry_file, size_bytes, json.dumps(metadata)),
                )
            conn.commit()
