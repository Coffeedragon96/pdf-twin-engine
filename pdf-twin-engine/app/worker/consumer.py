"""SQS-driven entrypoint for the pdf_twin job kind.

Long-polls the SQS-compatible queue named by SQS_QUEUE_PDF_TWIN, claims the
referenced processing_jobs row, runs the extract -> interpret -> reconstruct
pipeline, uploads the GLB, and writes the result exactly like every other
X2 worker kind (see x2-backend/worker/CONTRACT.md, "External processors").
"""

import logging
import math
import shutil
import tempfile
import threading
import time
import uuid as uuidlib
from pathlib import Path

import anthropic
import boto3

from app.core.config import settings
from app.core.db import Database
from app.services.emitter import emit_artifact
from app.services.extractor import extract_pdf
from app.services.interpreter import interpret_floor_plan
from app.services.reconstructor import COORD_SCALE, reconstruct_3d

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pdf-twin-engine")

VISIBILITY_TIMEOUT_S = 120
HEARTBEAT_INTERVAL_S = 45


def _has_usable_wall_geometry(floor_schema: dict) -> bool:
    """Mirrors reconstructor.reconstruct_3d's own wall-consumption logic
    (start/end present, each with >=2 coords, and a non-zero scaled length)
    so this check catches exactly the schemas that would otherwise trigger
    reconstructor's empty-mesh fallback, without duplicating its code."""
    for floor in floor_schema.get("floors", []) or []:
        for wall in floor.get("walls", []) or []:
            start = wall.get("start")
            end = wall.get("end")
            if not start or not end or len(start) < 2 or len(end) < 2:
                continue
            sx, sy = start[0] * COORD_SCALE, start[1] * COORD_SCALE
            ex, ey = end[0] * COORD_SCALE, end[1] * COORD_SCALE
            if math.hypot(ex - sx, ey - sy) >= 1e-4:
                return True
    return False


class Consumer:
    def __init__(self, sqs_client=None, db=None):
        self.db = db or Database()
        if sqs_client is not None:
            self.sqs = sqs_client
        else:
            kwargs = {"region_name": settings.SQS_REGION}
            if settings.SQS_ENDPOINT:
                kwargs["endpoint_url"] = settings.SQS_ENDPOINT
            if settings.S3_ACCESS_KEY:
                kwargs["aws_access_key_id"] = settings.S3_ACCESS_KEY
                kwargs["aws_secret_access_key"] = settings.S3_SECRET_KEY
            self.sqs = boto3.client("sqs", **kwargs)
        self.queue_url = self._resolve_queue_url(settings.SQS_QUEUE_PDF_TWIN)
        self._stopping = False

    def _resolve_queue_url(self, name: str) -> str:
        try:
            return self.sqs.get_queue_url(QueueName=name)["QueueUrl"]
        except Exception:  # noqa: BLE001 — QueueDoesNotExist is client-specific
            log.info("queue %s missing; creating (local convenience)", name)
            return self.sqs.create_queue(QueueName=name)["QueueUrl"]

    def run(self):
        log.info("pdf-twin-engine consumer started; queue=%s", self.queue_url)
        while not self._stopping:
            try:
                resp = self.sqs.receive_message(
                    QueueUrl=self.queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=20,
                    VisibilityTimeout=VISIBILITY_TIMEOUT_S,
                )
            except Exception:  # noqa: BLE001 — queue hiccups must not kill the loop
                log.exception("receive_message failed; retrying in 5s")
                time.sleep(5)
                continue
            for msg in resp.get("Messages", []):
                try:
                    self._process_message(msg)
                except Exception:  # noqa: BLE001 — one bad message must not kill the process
                    log.exception("unhandled error processing message; continuing")

    def _process_message(self, msg):
        receipt = msg["ReceiptHandle"]
        job_uuid = msg["Body"].strip()
        try:
            uuidlib.UUID(job_uuid)
        except ValueError:
            log.warning("dropping malformed message body: %r", msg["Body"][:100])
            self.sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=receipt)
            return

        job = self.db.claim_job(job_uuid)
        if job is None:
            log.info("job %s not claimable; dropping message", job_uuid)
            self.sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=receipt)
            return

        stop_beat = threading.Event()
        beat = threading.Thread(target=self._keepalive, args=(job["id"], receipt, stop_beat), daemon=True)
        beat.start()
        try:
            self._run_pipeline(job)
        except Exception as exc:  # noqa: BLE001 — every failure must reach the job row
            log.exception("job %s failed", job_uuid)
            self.db.fail(job, str(exc))
        finally:
            stop_beat.set()
            beat.join()
            self.sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=receipt)

    def _keepalive(self, job_id: int, receipt: str, stop: threading.Event):
        while not stop.wait(HEARTBEAT_INTERVAL_S):
            try:
                self.db.heartbeat(job_id)
                self.sqs.change_message_visibility(
                    QueueUrl=self.queue_url, ReceiptHandle=receipt,
                    VisibilityTimeout=VISIBILITY_TIMEOUT_S,
                )
            except Exception:  # noqa: BLE001 — a missed heartbeat must not crash the job
                log.exception("heartbeat/visibility-extend failed for job %s", job_id)

    def _run_pipeline(self, job: dict) -> None:
        options = job["input"].get("options") or {}
        metadata = {
            "building_name": options.get("building_name") or job["input"].get("original_filename", "Untitled"),
            "address": options.get("address"),
            "floors": options.get("floors"),
            "scale_meters": options.get("scale_meters"),
        }
        with tempfile.TemporaryDirectory(prefix="pdf-twin-") as scratch:
            pdf_path = Path(scratch) / "source.pdf"
            self._download_source(job, pdf_path)

            self.db.progress(job["id"], 15, "extracting")
            extraction = extract_pdf(str(pdf_path), metadata)

            self.db.progress(job["id"], 40, "interpreting")
            try:
                floor_schema = interpret_floor_plan(extraction, metadata)
            except anthropic.BadRequestError as exc:
                raise RuntimeError(
                    "Failed to interpret floor plan: Claude did not return a valid structured response"
                ) from exc

            if not _has_usable_wall_geometry(floor_schema):
                raise RuntimeError("No floor-plan geometry detected in the PDF")

            self.db.progress(job["id"], 65, "reconstructing")
            glb_path = Path(reconstruct_3d(job["job_uuid"], floor_schema))

            try:
                self.db.progress(job["id"], 85, "uploading")
                artifact_uuid = str(uuidlib.uuid4())
                root_path, size_bytes = emit_artifact(job, artifact_uuid, glb_path)

                self.db.succeed_with_artifact(
                    job,
                    artifact_uuid=artifact_uuid,
                    name=metadata["building_name"],
                    root_path=root_path,
                    entry_file="model.glb",
                    size_bytes=size_bytes,
                    metadata={
                        "floors": len(floor_schema.get("floors", [])),
                        "confidence": floor_schema.get("confidence", 0),
                        "building_name": metadata["building_name"],
                        "address": metadata["address"],
                        "scale_meters": metadata["scale_meters"],
                    },
                )
            finally:
                shutil.rmtree(glb_path.parent, ignore_errors=True)

    def _download_source(self, job: dict, dest: Path) -> None:
        kwargs = {"region_name": settings.S3_REGION}
        if settings.S3_ENDPOINT:
            kwargs["endpoint_url"] = settings.S3_ENDPOINT
        if settings.S3_ACCESS_KEY:
            kwargs["aws_access_key_id"] = settings.S3_ACCESS_KEY
            kwargs["aws_secret_access_key"] = settings.S3_SECRET_KEY
        s3 = boto3.client("s3", **kwargs)
        s3.download_file(job["input"]["source_bucket"], job["input"]["source_key"], str(dest))


if __name__ == "__main__":
    Consumer().run()
