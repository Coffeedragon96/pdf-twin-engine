import tempfile
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import anthropic
import pytest

from app.worker.consumer import Consumer, _select_floor_plan_candidates


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
    extraction = {"pages": [{"page_index": 0, "image_b64": "x", "width": 1, "height": 1}], "vector_geometry": None}
    classifications = [{"page_index": 0, "is_floor_plan": True, "floor_index": 0, "floor_label": "L1", "confidence": 0.9}]

    with patch("app.worker.consumer.boto3") as mock_boto3, \
         patch("app.worker.consumer.extract_pdf", return_value=extraction), \
         patch("app.worker.consumer.classify_pages", return_value=classifications), \
         patch("app.worker.consumer.interpret_floor_plan", return_value={"floors": [], "confidence": 0.1}), \
         patch("app.worker.consumer.reconstruct_3d") as mock_reconstruct, \
         patch("app.worker.consumer.emit_artifact") as mock_emit:
        mock_boto3.client.return_value = MagicMock()

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


def test_run_pipeline_skips_candidate_on_anthropic_bad_request_error():
    """A BadRequestError for the only candidate page is skip-and-continue
    (per-page interpretation failures never fail the job directly) — with
    only one candidate, zero floors survive, so the job still fails, but
    with the generic no-geometry message, not a BadRequestError-specific
    one (the specific reason is recorded in floors_skipped/logs instead)."""
    consumer, _sqs, db = _make_consumer()
    job = _make_job()
    extraction = {"pages": [{"page_index": 0, "image_b64": "x", "width": 1, "height": 1}], "vector_geometry": None}
    classifications = [{"page_index": 0, "is_floor_plan": True, "floor_index": 0, "floor_label": "L1", "confidence": 0.9}]

    bad_request = anthropic.BadRequestError(
        message="400 error from broken retry payload",
        response=MagicMock(status_code=400, headers={}),
        body={"error": {"message": "raw http garbage"}},
    )

    with patch("app.worker.consumer.boto3") as mock_boto3, \
         patch("app.worker.consumer.extract_pdf", return_value=extraction), \
         patch("app.worker.consumer.classify_pages", return_value=classifications), \
         patch("app.worker.consumer.interpret_floor_plan", side_effect=bad_request), \
         patch("app.worker.consumer.reconstruct_3d") as mock_reconstruct:
        mock_boto3.client.return_value = MagicMock()

        try:
            consumer._run_pipeline(job)
            assert False, "expected RuntimeError"
        except RuntimeError as exc:
            assert "No floor-plan geometry detected" in str(exc)

        mock_reconstruct.assert_not_called()

    # End-to-end through _process_message -> db.fail with that message.
    consumer2, sqs2, db2 = _make_consumer()
    db2.claim_job.return_value = job
    msg = {"Body": job["job_uuid"], "ReceiptHandle": "rB"}

    def _raise(_job):
        raise RuntimeError("No floor-plan geometry detected in the PDF")
    consumer2._run_pipeline = _raise

    consumer2._process_message(msg)

    db2.fail.assert_called_once()
    args, _ = db2.fail.call_args
    assert "No floor-plan geometry detected" in args[1]


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

    extraction = {"pages": [{"page_index": 0, "image_b64": "x", "width": 1, "height": 1}], "vector_geometry": None}
    classifications = [{"page_index": 0, "is_floor_plan": True, "floor_index": 0, "floor_label": "L1", "confidence": 0.9}]
    floor_schema = {
        "confidence": 0.92,
        "floors": [
            {"index": 0, "height_m": 3.0, "walls": [{"start": [0, 0], "end": [500, 0]}]}
        ],
    }

    with patch("app.worker.consumer.boto3") as mock_boto3, \
         patch("app.worker.consumer.extract_pdf", return_value=extraction), \
         patch("app.worker.consumer.classify_pages", return_value=classifications), \
         patch("app.worker.consumer.interpret_floor_plan", return_value=floor_schema), \
         patch("app.worker.consumer.reconstruct_3d", return_value=str(glb_path)) as mock_reconstruct, \
         patch("app.worker.consumer.emit_artifact", return_value=("org-uuid-1/artifact-1", 14)) as mock_emit:
        mock_boto3.client.return_value = MagicMock()

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
    assert kwargs["metadata"]["floors_identified"] == 1
    assert kwargs["metadata"]["floors_reconstructed"] == 1
    assert kwargs["metadata"]["floors_skipped"] == []

    # IMPORTANT 3: glb_path.parent must be removed after the run, win or lose.
    assert not glb_dir.exists()


def test_select_floor_plan_candidates_orders_by_floor_index():
    classifications = [
        {"page_index": 0, "is_floor_plan": False, "floor_index": 0, "floor_label": "COVER", "confidence": 0.9},
        {"page_index": 2, "is_floor_plan": True, "floor_index": 1, "floor_label": "L2", "confidence": 0.9},
        {"page_index": 1, "is_floor_plan": True, "floor_index": 0, "floor_label": "GROUND", "confidence": 0.9},
    ]

    candidates, duplicates = _select_floor_plan_candidates(classifications)

    assert [c["page_index"] for c in candidates] == [1, 2]
    assert duplicates == []


def test_select_floor_plan_candidates_dedupes_by_floor_index():
    classifications = [
        {"page_index": 0, "is_floor_plan": True, "floor_index": 0, "floor_label": "GROUND A", "confidence": 0.6},
        {"page_index": 1, "is_floor_plan": True, "floor_index": 0, "floor_label": "GROUND B", "confidence": 0.9},
    ]

    candidates, duplicates = _select_floor_plan_candidates(classifications)

    assert len(candidates) == 1
    assert candidates[0]["page_index"] == 1
    assert len(duplicates) == 1
    assert duplicates[0]["page_index"] == 0


def test_select_floor_plan_candidates_returns_empty_for_no_floor_plans():
    classifications = [{"page_index": 0, "is_floor_plan": False, "floor_index": 0, "floor_label": "", "confidence": 0.9}]

    candidates, duplicates = _select_floor_plan_candidates(classifications)

    assert candidates == []
    assert duplicates == []


def test_run_pipeline_fails_when_no_floor_plan_pages_identified():
    consumer, _sqs, db = _make_consumer()
    job = _make_job()
    extraction = {"pages": [{"page_index": 0, "image_b64": "x", "width": 1, "height": 1}], "vector_geometry": None}
    classifications = [{"page_index": 0, "is_floor_plan": False, "floor_index": 0, "floor_label": "COVER", "confidence": 0.9}]

    with patch("app.worker.consumer.boto3") as mock_boto3, \
         patch("app.worker.consumer.extract_pdf", return_value=extraction), \
         patch("app.worker.consumer.classify_pages", return_value=classifications):
        mock_boto3.client.return_value = MagicMock()

        with pytest.raises(RuntimeError, match="No floor-plan geometry detected"):
            consumer._run_pipeline(job)


def test_run_pipeline_skips_candidate_with_empty_floors_list(tmp_path):
    consumer, _sqs, db = _make_consumer()
    job = _make_job()
    extraction = {
        "pages": [
            {"page_index": 0, "image_b64": "x", "width": 1, "height": 1},
            {"page_index": 1, "image_b64": "y", "width": 1, "height": 1},
        ],
        "vector_geometry": None,
    }
    classifications = [
        {"page_index": 0, "is_floor_plan": True, "floor_index": 0, "floor_label": "L1", "confidence": 0.9},
        {"page_index": 1, "is_floor_plan": True, "floor_index": 1, "floor_label": "L2", "confidence": 0.9},
    ]
    good_schema = {"confidence": 0.9, "floors": [{"index": 0, "height_m": 3.0, "walls": [{"start": [0, 0], "end": [100, 0]}]}]}
    empty_schema = {"confidence": 0.5, "floors": []}

    glb_path = tmp_path / "model.glb"
    glb_path.write_bytes(b"x")

    with patch("app.worker.consumer.boto3") as mock_boto3, \
         patch("app.worker.consumer.extract_pdf", return_value=extraction), \
         patch("app.worker.consumer.classify_pages", return_value=classifications), \
         patch("app.worker.consumer.interpret_floor_plan", side_effect=[good_schema, empty_schema]), \
         patch("app.worker.consumer.reconstruct_3d", return_value=str(glb_path)), \
         patch("app.worker.consumer.emit_artifact", return_value=("root", 10)):
        mock_boto3.client.return_value = MagicMock()
        consumer._run_pipeline(job)

    _args, kwargs = consumer.db.succeed_with_artifact.call_args
    assert kwargs["metadata"]["floors_reconstructed"] == 1
    assert kwargs["metadata"]["floors_identified"] == 2
    assert kwargs["metadata"]["floors_skipped"][0]["page_index"] == 1
    assert kwargs["metadata"]["floors_skipped"][0]["reason"] == "no_usable_geometry"


def test_run_pipeline_single_floor_plan_page_still_works(tmp_path):
    consumer, _sqs, db = _make_consumer()
    job = _make_job()
    extraction = {"pages": [{"page_index": 0, "image_b64": "x", "width": 1, "height": 1}], "vector_geometry": None}
    classifications = [{"page_index": 0, "is_floor_plan": True, "floor_index": 0, "floor_label": "L1", "confidence": 0.9}]
    schema = {"confidence": 0.9, "floors": [{"index": 0, "height_m": 3.0, "walls": [{"start": [0, 0], "end": [100, 0]}]}]}

    glb_path = tmp_path / "model.glb"
    glb_path.write_bytes(b"x")

    with patch("app.worker.consumer.boto3") as mock_boto3, \
         patch("app.worker.consumer.extract_pdf", return_value=extraction), \
         patch("app.worker.consumer.classify_pages", return_value=classifications), \
         patch("app.worker.consumer.interpret_floor_plan", return_value=schema) as mock_interpret, \
         patch("app.worker.consumer.reconstruct_3d", return_value=str(glb_path)) as mock_reconstruct, \
         patch("app.worker.consumer.emit_artifact", return_value=("root", 10)):
        mock_boto3.client.return_value = MagicMock()
        consumer._run_pipeline(job)

    mock_interpret.assert_called_once()
    reconstruct_floor_schema = mock_reconstruct.call_args.args[1]
    assert len(reconstruct_floor_schema["floors"]) == 1
    assert reconstruct_floor_schema["floors"][0]["index"] == 0
    kwargs = consumer.db.succeed_with_artifact.call_args.kwargs
    assert kwargs["metadata"]["floors_reconstructed"] == 1
    assert kwargs["metadata"]["floors_skipped"] == []


def test_run_pipeline_raises_distinct_error_when_classification_fails_for_every_page():
    """Important 1 (final review): if every page's classification call
    fails (e.g. an invalid API key), the job must not be reported as
    'no floor-plan geometry' — that points the user at their document when
    the real cause is the API/credentials."""
    consumer, _sqs, db = _make_consumer()
    job = _make_job()
    extraction = {
        "pages": [
            {"page_index": 0, "image_b64": "x", "width": 1, "height": 1},
            {"page_index": 1, "image_b64": "y", "width": 1, "height": 1},
        ],
        "vector_geometry": None,
    }
    classifications = [
        {"page_index": 0, "is_floor_plan": False, "floor_index": 0, "floor_label": "", "confidence": 0.0, "error": "API key is invalid."},
        {"page_index": 1, "is_floor_plan": False, "floor_index": 0, "floor_label": "", "confidence": 0.0, "error": "API key is invalid."},
    ]

    with patch("app.worker.consumer.boto3") as mock_boto3, \
         patch("app.worker.consumer.extract_pdf", return_value=extraction), \
         patch("app.worker.consumer.classify_pages", return_value=classifications):
        mock_boto3.client.return_value = MagicMock()

        with pytest.raises(RuntimeError, match="classification failed for all 2 pages"):
            consumer._run_pipeline(job)


def test_run_pipeline_records_classification_failures_in_skipped_metadata():
    """Important 2 (final review): a page whose classification call itself
    failed (not just a clean 'not a floor plan' answer) must leave a trace
    in floors_skipped, even when the job still succeeds using other pages."""
    consumer, _sqs, db = _make_consumer()
    job = _make_job()
    extraction = {
        "pages": [
            {"page_index": 0, "image_b64": "x", "width": 1, "height": 1},
            {"page_index": 1, "image_b64": "y", "width": 1, "height": 1},
            {"page_index": 2, "image_b64": "z", "width": 1, "height": 1},
        ],
        "vector_geometry": None,
    }
    classifications = [
        {"page_index": 0, "is_floor_plan": False, "floor_index": 0, "floor_label": "", "confidence": 0.0, "error": "network error"},
        {"page_index": 1, "is_floor_plan": True, "floor_index": 0, "floor_label": "L1", "confidence": 0.9},
        {"page_index": 2, "is_floor_plan": False, "floor_index": 0, "floor_label": "COVER", "confidence": 0.9},
    ]
    schema = {"confidence": 0.9, "floors": [{"index": 0, "height_m": 3.0, "walls": [{"start": [0, 0], "end": [100, 0]}]}]}
    glb_path_holder = {}

    def _reconstruct(_job_id, _schema):
        return glb_path_holder["path"]

    with patch("app.worker.consumer.boto3") as mock_boto3, \
         patch("app.worker.consumer.extract_pdf", return_value=extraction), \
         patch("app.worker.consumer.classify_pages", return_value=classifications), \
         patch("app.worker.consumer.interpret_floor_plan", return_value=schema), \
         patch("app.worker.consumer.reconstruct_3d", side_effect=_reconstruct), \
         patch("app.worker.consumer.emit_artifact", return_value=("root", 10)), \
         tempfile.TemporaryDirectory() as tmp:
        glb_path_holder["path"] = str(Path(tmp) / "model.glb")
        Path(glb_path_holder["path"]).write_bytes(b"x")
        mock_boto3.client.return_value = MagicMock()
        consumer._run_pipeline(job)

    kwargs = consumer.db.succeed_with_artifact.call_args.kwargs
    skipped_reasons = {(s["page_index"], s["reason"]) for s in kwargs["metadata"]["floors_skipped"]}
    assert (0, "classification_failed") in skipped_reasons
    assert (2, "classification_failed") not in skipped_reasons  # a clean non-floor-plan answer is not a failure
    assert kwargs["metadata"]["floors_reconstructed"] == 1


def test_run_pipeline_combines_two_floor_plan_candidates_in_order():
    """The spec's core multi-floor orchestration case: one non-floor-plan
    page plus two floor-plan pages with different floor_index must combine
    into one floors[] of length 2, ordered and indexed by classification,
    not by page order."""
    consumer, _sqs, db = _make_consumer()
    job = _make_job()
    extraction = {
        "pages": [
            {"page_index": 0, "image_b64": "cover", "width": 1, "height": 1},
            {"page_index": 1, "image_b64": "l2", "width": 1, "height": 1},
            {"page_index": 2, "image_b64": "l1", "width": 1, "height": 1},
        ],
        "vector_geometry": None,
    }
    classifications = [
        {"page_index": 0, "is_floor_plan": False, "floor_index": 0, "floor_label": "COVER", "confidence": 0.9},
        {"page_index": 1, "is_floor_plan": True, "floor_index": 1, "floor_label": "LEVEL 2", "confidence": 0.9},
        {"page_index": 2, "is_floor_plan": True, "floor_index": 0, "floor_label": "GROUND", "confidence": 0.9},
    ]
    schema_for_page = {
        1: {"confidence": 0.9, "floors": [{"index": 0, "height_m": 3.0, "walls": [{"start": [0, 0], "end": [100, 0]}]}]},
        2: {"confidence": 0.9, "floors": [{"index": 0, "height_m": 3.0, "walls": [{"start": [0, 0], "end": [200, 0]}]}]},
    }

    def _interpret(page_extraction, _metadata):
        page_index = page_extraction["pages"][0]["page_index"]
        return schema_for_page[page_index]

    with patch("app.worker.consumer.boto3") as mock_boto3, \
         patch("app.worker.consumer.extract_pdf", return_value=extraction), \
         patch("app.worker.consumer.classify_pages", return_value=classifications), \
         patch("app.worker.consumer.interpret_floor_plan", side_effect=_interpret), \
         patch("app.worker.consumer.reconstruct_3d", return_value="/tmp/does-not-matter/model.glb") as mock_reconstruct, \
         patch("app.worker.consumer.emit_artifact", return_value=("root", 10)), \
         patch("app.worker.consumer.shutil.rmtree"):
        mock_boto3.client.return_value = MagicMock()
        consumer._run_pipeline(job)

    reconstruct_floor_schema = mock_reconstruct.call_args.args[1]
    floors = reconstruct_floor_schema["floors"]
    assert [f["index"] for f in floors] == [0, 1]
    assert floors[0]["walls"][0]["end"] == [200, 0]  # page 2 -> floor_index 0
    assert floors[1]["walls"][0]["end"] == [100, 0]  # page 1 -> floor_index 1


def test_run_pipeline_continues_after_one_of_multiple_candidates_fails_interpretation():
    """A failing candidate among several must not take the others down with
    it — the earlier single-candidate exception test couldn't show this
    since the loop had nothing left to continue to."""
    consumer, _sqs, db = _make_consumer()
    job = _make_job()
    extraction = {
        "pages": [
            {"page_index": 0, "image_b64": "x", "width": 1, "height": 1},
            {"page_index": 1, "image_b64": "y", "width": 1, "height": 1},
        ],
        "vector_geometry": None,
    }
    classifications = [
        {"page_index": 0, "is_floor_plan": True, "floor_index": 0, "floor_label": "L1", "confidence": 0.9},
        {"page_index": 1, "is_floor_plan": True, "floor_index": 1, "floor_label": "L2", "confidence": 0.9},
    ]
    good_schema = {"confidence": 0.9, "floors": [{"index": 0, "height_m": 3.0, "walls": [{"start": [0, 0], "end": [100, 0]}]}]}

    with patch("app.worker.consumer.boto3") as mock_boto3, \
         patch("app.worker.consumer.extract_pdf", return_value=extraction), \
         patch("app.worker.consumer.classify_pages", return_value=classifications), \
         patch("app.worker.consumer.interpret_floor_plan", side_effect=[RuntimeError("boom"), good_schema]), \
         patch("app.worker.consumer.reconstruct_3d", return_value="/tmp/does-not-matter/model.glb"), \
         patch("app.worker.consumer.emit_artifact", return_value=("root", 10)), \
         patch("app.worker.consumer.shutil.rmtree"):
        mock_boto3.client.return_value = MagicMock()
        consumer._run_pipeline(job)

    kwargs = consumer.db.succeed_with_artifact.call_args.kwargs
    assert kwargs["metadata"]["floors_identified"] == 2
    assert kwargs["metadata"]["floors_reconstructed"] == 1
    assert kwargs["metadata"]["floors_skipped"][0]["page_index"] == 0
    assert kwargs["metadata"]["floors_skipped"][0]["reason"] == "interpretation_failed"


def test_run_pipeline_duplicate_floor_index_recorded_in_skipped_metadata_end_to_end():
    """The plan's own Review Focus wording: a duplicate floor_index loser
    must be recorded, not discarded without a trace — verified through the
    real _run_pipeline, not just _select_floor_plan_candidates in isolation."""
    consumer, _sqs, db = _make_consumer()
    job = _make_job()
    extraction = {
        "pages": [
            {"page_index": 0, "image_b64": "x", "width": 1, "height": 1},
            {"page_index": 1, "image_b64": "y", "width": 1, "height": 1},
        ],
        "vector_geometry": None,
    }
    classifications = [
        {"page_index": 0, "is_floor_plan": True, "floor_index": 0, "floor_label": "GROUND A", "confidence": 0.6},
        {"page_index": 1, "is_floor_plan": True, "floor_index": 0, "floor_label": "GROUND B", "confidence": 0.9},
    ]
    schema = {"confidence": 0.9, "floors": [{"index": 0, "height_m": 3.0, "walls": [{"start": [0, 0], "end": [100, 0]}]}]}

    with patch("app.worker.consumer.boto3") as mock_boto3, \
         patch("app.worker.consumer.extract_pdf", return_value=extraction), \
         patch("app.worker.consumer.classify_pages", return_value=classifications), \
         patch("app.worker.consumer.interpret_floor_plan", return_value=schema) as mock_interpret, \
         patch("app.worker.consumer.reconstruct_3d", return_value="/tmp/does-not-matter/model.glb"), \
         patch("app.worker.consumer.emit_artifact", return_value=("root", 10)), \
         patch("app.worker.consumer.shutil.rmtree"):
        mock_boto3.client.return_value = MagicMock()
        consumer._run_pipeline(job)

    mock_interpret.assert_called_once()  # only the winner is ever interpreted
    kwargs = consumer.db.succeed_with_artifact.call_args.kwargs
    assert kwargs["metadata"]["floors_identified"] == 1
    assert kwargs["metadata"]["floors_reconstructed"] == 1
    assert kwargs["metadata"]["floors_skipped"] == [{"page_index": 0, "floor_label": "GROUND A", "reason": "duplicate_floor_index"}]
