"""Contract tests for app.core.db, mirroring x2-backend/worker's own
Database tests (Mock-based, no live Postgres required)."""

from unittest.mock import MagicMock, patch

from app.core.db import Database, _decode_jsonb


def test_jsonb_dict_passes_through():
    assert _decode_jsonb({"source_key": "a/b.pdf"}) == {"source_key": "a/b.pdf"}


def test_jsonb_text_is_parsed():
    assert _decode_jsonb('{"source_key": "a/b.pdf"}') == {"source_key": "a/b.pdf"}


def test_jsonb_null_becomes_empty_dict():
    assert _decode_jsonb(None) == {}


def _mock_connection(fetchone_return=None, rowcount=1):
    cursor = MagicMock()
    cursor.fetchone.return_value = fetchone_return
    cursor.rowcount = rowcount
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value.__enter__.return_value = cursor
    return conn, cursor


def test_claim_job_returns_none_when_not_queued():
    db = Database()
    conn, _ = _mock_connection(fetchone_return=None)
    with patch.object(db, "connect", return_value=conn):
        assert db.claim_job("11111111-1111-1111-1111-111111111111") is None


def test_claim_job_returns_job_dict_when_claimed():
    db = Database()
    conn, cursor = _mock_connection(fetchone_return=(5, 1, 2, "pdf_twin", '{"options":{}}'))
    cursor.fetchone.side_effect = [(5, 1, 2, "pdf_twin", '{"options":{}}'), ("org-uuid-1",)]
    with patch.object(db, "connect", return_value=conn):
        job = db.claim_job("11111111-1111-1111-1111-111111111111")
    assert job == {
        "id": 5,
        "job_uuid": "11111111-1111-1111-1111-111111111111",
        "org_id": 1,
        "project_id": 2,
        "kind": "pdf_twin",
        "input": {"options": {}},
        "org_uuid": "org-uuid-1",
    }


def test_succeed_with_artifact_raises_if_job_left_running_state():
    db = Database()
    conn, _ = _mock_connection(rowcount=0)
    with patch("app.core.db.psycopg.connect", return_value=conn):
        try:
            db.succeed_with_artifact(
                {"id": 1, "org_id": 1, "project_id": 1}, "artifact-uuid", "name",
                "root", "model.glb", 10, {},
            )
            raise AssertionError("expected RuntimeError")
        except RuntimeError as exc:
            assert "canceled" in str(exc)
