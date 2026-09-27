from pathlib import Path
from unittest.mock import MagicMock, patch

from app.services.emitter import emit_artifact


def test_emit_artifact_uploads_to_governed_root_path_and_returns_size(tmp_path):
    glb_path = tmp_path / "model.glb"
    glb_path.write_bytes(b"\x00" * 128)
    job = {"org_uuid": "org-uuid-1"}

    fake_s3 = MagicMock()
    with patch("app.services.emitter.boto3.client", return_value=fake_s3):
        root_path, size_bytes = emit_artifact(job, "artifact-uuid-1", glb_path)

    assert root_path == "org-uuid-1/artifact-uuid-1"
    assert size_bytes == 128
    fake_s3.upload_file.assert_called_once()
    call = fake_s3.upload_file.call_args
    assert call.kwargs["Bucket"]
    assert call.kwargs["Key"] == "org-uuid-1/artifact-uuid-1/model.glb"
    assert call.kwargs["ExtraArgs"]["ContentType"] == "model/gltf-binary"


def test_emit_artifact_raises_if_glb_missing(tmp_path):
    job = {"org_uuid": "org-uuid-1"}
    try:
        emit_artifact(job, "artifact-uuid-1", tmp_path / "missing.glb")
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "not found" in str(exc)
