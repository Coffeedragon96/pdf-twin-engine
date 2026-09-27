"""Uploads the generated GLB to the governed artifact root_path every X2
artifact uses (see x2-backend/worker/CONTRACT.md, "Artifacts")."""

import logging
from pathlib import Path
from typing import Tuple

import boto3

from app.core.config import settings

logger = logging.getLogger("pdf-twin-engine")


def emit_artifact(job: dict, artifact_uuid: str, glb_path: Path) -> Tuple[str, int]:
    """Uploads glb_path to the artifacts bucket and returns (root_path,
    size_bytes) for the row `Database.succeed_with_artifact` inserts."""
    if not glb_path.exists():
        raise RuntimeError(f"GLB file not found at: {glb_path}")

    kwargs = {"region_name": settings.S3_REGION}
    if settings.S3_ENDPOINT:
        kwargs["endpoint_url"] = settings.S3_ENDPOINT
    if settings.S3_ACCESS_KEY:
        kwargs["aws_access_key_id"] = settings.S3_ACCESS_KEY
        kwargs["aws_secret_access_key"] = settings.S3_SECRET_KEY
    s3 = boto3.client("s3", **kwargs)

    root_path = f"{job['org_uuid']}/{artifact_uuid}"
    key = f"{root_path}/model.glb"
    size_bytes = glb_path.stat().st_size

    s3.upload_file(
        Filename=str(glb_path),
        Bucket=settings.S3_BUCKET_ARTIFACTS,
        Key=key,
        ExtraArgs={"ContentType": "model/gltf-binary"},
    )
    logger.info("uploaded artifact %s (%d bytes) to %s", artifact_uuid, size_bytes, key)
    return root_path, size_bytes
