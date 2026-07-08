import logging
from pathlib import Path
from typing import Dict, Any, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import settings

logger = logging.getLogger(__name__)


def emit_artifact(job_id: str, glb_path: str, floor_schema: Dict[str, Any]) -> str:
    """
    Uploads the generated GLB file to AWS S3 and returns a public download URL.

    If S3 credentials are not configured (local/dev environment),
    returns a local file path as the result URL instead.

    Args:
        job_id:       Unique job identifier.
        glb_path:     Absolute path to the generated GLB file.
        floor_schema: Structured floor schema (used for metadata tagging).

    Returns:
        Public S3 URL or local file path to the GLB model.

    Raises:
        RuntimeError: If the S3 upload fails.
    """
    glb_file = Path(glb_path)

    if not glb_file.exists():
        raise RuntimeError(f"GLB file not found at: {glb_path}")

    # Dev mode - no S3 configured, return local path
    if not settings.AWS_S3_BUCKET or not settings.AWS_ACCESS_KEY_ID:
        logger.warning(
            "S3 credentials not configured. Returning local path for job %s", job_id
        )
        return f"file://{glb_path}"

    s3_key = f"twins/{job_id}/model.glb"

    try:
        s3_client = boto3.client(
            "s3",
            region_name            = settings.AWS_S3_REGION,
            aws_access_key_id      = settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key  = settings.AWS_SECRET_ACCESS_KEY,
        )

        # Upload GLB with correct MIME type for WebGL/Cesium compatibility
        s3_client.upload_file(
            Filename    = str(glb_file),
            Bucket      = settings.AWS_S3_BUCKET,
            Key         = s3_key,
            ExtraArgs   = {
                "ContentType":  "model/gltf-binary",
                "ContentDisposition": f'attachment; filename="model_{job_id}.glb"',
                "Metadata": {
                    "job_id":    job_id,
                    "floors":    str(len(floor_schema.get("floors", []))),
                    "confidence": str(floor_schema.get("confidence", 0)),
                },
            },
        )

        result_url = (
            f"https://{settings.AWS_S3_BUCKET}.s3."
            f"{settings.AWS_S3_REGION}.amazonaws.com/{s3_key}"
        )

        logger.info("GLB uploaded to S3: %s", result_url)

        # Cleanup local GLB after successful upload
        _cleanup_local(glb_file)

        return result_url

    except (BotoCoreError, ClientError) as exc:
        logger.error("S3 upload failed for job %s: %s", job_id, str(exc))
        raise RuntimeError(f"S3 upload failed: {exc}") from exc


def _cleanup_local(glb_file: Path) -> None:
    """Removes the local GLB file and its parent directory after S3 upload."""
    try:
        glb_file.unlink(missing_ok=True)
        parent = glb_file.parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
        logger.info("Cleaned up local output: %s", glb_file)
    except Exception as exc:
        logger.warning("Cleanup failed (non-critical): %s", exc)