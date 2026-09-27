import uuid
from unittest.mock import MagicMock

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
