import uuid
from unittest.mock import MagicMock, patch

import anthropic
import pytest

from app.worker.consumer import Consumer


def _make_consumer():
    sqs = MagicMock()
    sqs.get_queue_url.return_value = {"QueueUrl": "https://queue/pdf-twin"}
    db = MagicMock()
    return Consumer(sqs_client=sqs, db=db), sqs, db


def test_process_message_drops_malformed_body():
    consumer, sqs, db = _make_consumer()
    msg = {"Body": "not-a-uuid", "ReceiptHandle": "r1"}

    consumer._process_message(msg)

    db.claim_job.assert_not_called()
    sqs.delete_message.assert_called_once_with(QueueUrl="https://queue/pdf-twin", ReceiptHandle="r1")


def test_process_message_drops_when_job_not_claimable():
    consumer, sqs, db = _make_consumer()
    db.claim_job.return_value = None
    job_uuid = str(uuid.uuid4())
    msg = {"Body": job_uuid, "ReceiptHandle": "r2"}

    consumer._process_message(msg)

    db.claim_job.assert_called_once_with(job_uuid)
    sqs.delete_message.assert_called_once_with(QueueUrl="https://queue/pdf-twin", ReceiptHandle="r2")


def test_process_message_marks_job_failed_on_pipeline_error():
    consumer, sqs, db = _make_consumer()
    job_uuid = str(uuid.uuid4())
    job = {"id": 1, "job_uuid": job_uuid, "org_id": 1, "project_id": 1, "org_uuid": "o", "input": {}}
    db.claim_job.return_value = job
    msg = {"Body": job_uuid, "ReceiptHandle": "r3"}

    def _boom(_job):
        raise RuntimeError("pipeline exploded")
    consumer._run_pipeline = _boom

    consumer._process_message(msg)

    db.fail.assert_called_once()
    args, _ = db.fail.call_args
    assert args[0] is job
    assert "pipeline exploded" in args[1]
    sqs.delete_message.assert_called_once_with(QueueUrl="https://queue/pdf-twin", ReceiptHandle="r3")


def test_run_survives_exception_from_process_message_and_continues():
    consumer, sqs, db = _make_consumer()
    msg1 = {"Body": "boom", "ReceiptHandle": "r5"}
    msg2 = {"Body": "ok", "ReceiptHandle": "r6"}

    calls = []

    def _process_message(msg):
        calls.append(msg)
        if msg is msg1:
            raise RuntimeError("boom")

    consumer._process_message = _process_message

    served = {"done": False}

    def _receive(*args, **kwargs):
        if not served["done"]:
            served["done"] = True
            return {"Messages": [msg1, msg2]}
        consumer._stopping = True
        return {"Messages": []}

    sqs.receive_message.side_effect = _receive

    consumer.run()

    assert calls == [msg1, msg2]


def test_process_message_does_not_fail_job_on_success():
    consumer, sqs, db = _make_consumer()
    job_uuid = str(uuid.uuid4())
    job = {"id": 1, "job_uuid": job_uuid, "org_id": 1, "project_id": 1, "org_uuid": "o", "input": {}}
    db.claim_job.return_value = job
    msg = {"Body": job_uuid, "ReceiptHandle": "r4"}

    consumer._run_pipeline = lambda _job: None

    consumer._process_message(msg)

    db.fail.assert_not_called()
    sqs.delete_message.assert_called_once_with(QueueUrl="https://queue/pdf-twin", ReceiptHandle="r4")


def _make_job():
    job_uuid = str(uuid.uuid4())
    return {
        "id": 1,
        "job_uuid": job_uuid,
        "org_id": 1,
        "project_id": 1,
        "org_uuid": "org-uuid-1",
        "input": {
            "source_bucket": "uploads",
            "source_key": "some/key.pdf",
            "original_filename": "plan.pdf",
            "options": {"building_name": "Tower A", "address": "1 Main St", "floors": 2, "scale_meters": 1.0},
        },
    }


def test_run_pipeline_rejects_schema_with_no_usable_walls():
    """CRITICAL 1: a floor schema with no usable wall geometry must fail the
    job instead of letting reconstructor's empty-mesh fallback succeed."""
    consumer, _sqs, db = _make_consumer()
    job = _make_job()

    with patch("app.worker.consumer.boto3") as mock_boto3, \
         patch("app.worker.consumer.extract_pdf") as mock_extract, \
         patch("app.worker.consumer.interpret_floor_plan") as mock_interpret, \
         patch("app.worker.consumer.reconstruct_3d") as mock_reconstruct, \
         patch("app.worker.consumer.emit_artifact") as mock_emit:
        mock_boto3.client.return_value = MagicMock()
        mock_extract.return_value = {"pages": []}
        mock_interpret.return_value = {"floors": [], "confidence": 0.1}

        with pytest.raises(RuntimeError, match="No floor-plan geometry detected"):
            consumer._run_pipeline(job)

        mock_reconstruct.assert_not_called()
        mock_emit.assert_not_called()

    # Through the normal dispatch path, this should land as a db.fail() call.
    consumer2, sqs2, db2 = _make_consumer()
    db2.claim_job.return_value = job
    msg = {"Body": job["job_uuid"], "ReceiptHandle": "rW"}

    def _boom(_job):
        raise RuntimeError("No floor-plan geometry detected in the PDF")
    consumer2._run_pipeline = _boom

    consumer2._process_message(msg)

    db2.fail.assert_called_once()
    args, _ = db2.fail.call_args
    assert "No floor-plan geometry detected" in args[1]
    db2.succeed_with_artifact.assert_not_called()


def test_run_pipeline_wraps_anthropic_bad_request_error():
    """IMPORTANT 4: a BadRequestError from interpret_floor_plan must be
    rewritten into a clear message before it reaches db.fail."""
    consumer, _sqs, db = _make_consumer()
    job = _make_job()

    bad_request = anthropic.BadRequestError(
        message="400 error from broken retry payload",
        response=MagicMock(status_code=400, headers={}),
        body={"error": {"message": "raw http garbage"}},
    )

    with patch("app.worker.consumer.boto3") as mock_boto3, \
         patch("app.worker.consumer.extract_pdf") as mock_extract, \
         patch("app.worker.consumer.interpret_floor_plan", side_effect=bad_request), \
         patch("app.worker.consumer.reconstruct_3d") as mock_reconstruct:
        mock_boto3.client.return_value = MagicMock()
        mock_extract.return_value = {"pages": []}

        try:
            consumer._run_pipeline(job)
            assert False, "expected RuntimeError"
        except RuntimeError as exc:
            assert "Failed to interpret floor plan" in str(exc)
            assert "did not return a valid structured response" in str(exc)

        mock_reconstruct.assert_not_called()

    # End-to-end through _process_message -> db.fail with the clear message.
    consumer2, sqs2, db2 = _make_consumer()
    db2.claim_job.return_value = job
    msg = {"Body": job["job_uuid"], "ReceiptHandle": "rB"}

    def _raise(_job):
        raise RuntimeError(
            "Failed to interpret floor plan: Claude did not return a valid structured response"
        )
    consumer2._run_pipeline = _raise

    consumer2._process_message(msg)

    db2.fail.assert_called_once()
    args, _ = db2.fail.call_args
    assert "Failed to interpret floor plan" in args[1]


def test_run_pipeline_success_calls_succeed_with_artifact_and_cleans_up(tmp_path):
    """IMPORTANT 3 + 5: exercise the real _run_pipeline success path end to
    end and confirm both the succeed_with_artifact call shape and the
    post-run cleanup of the GLB output directory."""
    consumer, _sqs, db = _make_consumer()
    job = _make_job()

    glb_dir = tmp_path / "pdf_twin_outputs" / job["job_uuid"]
    glb_dir.mkdir(parents=True)
    glb_path = glb_dir / "model.glb"
    glb_path.write_bytes(b"fake-glb-bytes")

    floor_schema = {
        "confidence": 0.92,
        "floors": [
            {"index": 0, "height_m": 3.0, "walls": [{"start": [0, 0], "end": [500, 0]}]}
        ],
    }

    with patch("app.worker.consumer.boto3") as mock_boto3, \
         patch("app.worker.consumer.extract_pdf") as mock_extract, \
         patch("app.worker.consumer.interpret_floor_plan", return_value=floor_schema), \
         patch("app.worker.consumer.reconstruct_3d", return_value=str(glb_path)) as mock_reconstruct, \
         patch("app.worker.consumer.emit_artifact", return_value=("org-uuid-1/artifact-1", 14)) as mock_emit:
        mock_boto3.client.return_value = MagicMock()
        mock_extract.return_value = {"pages": []}

        consumer._run_pipeline(job)

    mock_reconstruct.assert_called_once()
    mock_emit.assert_called_once()

    db.succeed_with_artifact.assert_called_once()
    _args, kwargs = db.succeed_with_artifact.call_args
    assert kwargs["entry_file"] == "model.glb"
    assert kwargs["root_path"] == "org-uuid-1/artifact-1"
    assert kwargs["name"] == "Tower A"
    assert kwargs["metadata"]["floors"] == 1
    assert kwargs["metadata"]["confidence"] == 0.92
    assert kwargs["metadata"]["building_name"] == "Tower A"
    assert kwargs["metadata"]["address"] == "1 Main St"
    assert kwargs["metadata"]["scale_meters"] == 1.0

    # IMPORTANT 3: glb_path.parent must be removed after the run, win or lose.
    assert not glb_dir.exists()
