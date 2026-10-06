import json
from unittest.mock import MagicMock, patch

from app.services.classifier import classify_pages


def _fake_response(text: str):
    content_block = MagicMock()
    content_block.text = text
    response = MagicMock()
    response.content = [content_block]
    return response


def _extraction_with_pages(*page_indices):
    return {
        "pages": [{"page_index": i, "image_b64": f"fake-b64-{i}", "width": 100, "height": 100} for i in page_indices],
    }


def test_classify_pages_returns_one_result_per_page_in_order():
    extraction = _extraction_with_pages(0, 1, 2)
    responses = [
        _fake_response(json.dumps({"is_floor_plan": False, "floor_index": 0, "floor_label": "COVER", "confidence": 0.95})),
        _fake_response(json.dumps({"is_floor_plan": True, "floor_index": 0, "floor_label": "GROUND FLOOR", "confidence": 0.9})),
        _fake_response(json.dumps({"is_floor_plan": True, "floor_index": 1, "floor_label": "LEVEL 2", "confidence": 0.85})),
    ]
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = responses

    with patch("app.services.classifier.anthropic.Anthropic", return_value=mock_client):
        results = classify_pages(extraction, {"building_name": "Test"})

    assert [r["page_index"] for r in results] == [0, 1, 2]
    assert results[0]["is_floor_plan"] is False
    assert results[1] == {"page_index": 1, "is_floor_plan": True, "floor_index": 0, "floor_label": "GROUND FLOOR", "confidence": 0.9}
    assert results[2]["floor_index"] == 1
    assert mock_client.messages.create.call_count == 3


def test_classify_pages_strips_markdown_fences():
    extraction = _extraction_with_pages(0)
    response = _fake_response("```json\n" + json.dumps({"is_floor_plan": True, "floor_index": 0, "floor_label": "L1", "confidence": 0.8}) + "\n```")
    mock_client = MagicMock()
    mock_client.messages.create.return_value = response

    with patch("app.services.classifier.anthropic.Anthropic", return_value=mock_client):
        results = classify_pages(extraction, {})

    assert results[0]["is_floor_plan"] is True
    assert results[0]["floor_label"] == "L1"


def test_classify_pages_treats_call_failure_as_non_floor_plan():
    extraction = _extraction_with_pages(0, 1)
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        RuntimeError("network error"),
        _fake_response(json.dumps({"is_floor_plan": True, "floor_index": 0, "floor_label": "L1", "confidence": 0.9})),
    ]

    with patch("app.services.classifier.anthropic.Anthropic", return_value=mock_client):
        results = classify_pages(extraction, {})

    assert results[0]["is_floor_plan"] is False
    assert "network error" in results[0]["error"]
    assert results[1]["is_floor_plan"] is True


def test_classify_pages_treats_malformed_json_as_non_floor_plan():
    extraction = _extraction_with_pages(0)
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _fake_response("not json at all")

    with patch("app.services.classifier.anthropic.Anthropic", return_value=mock_client):
        results = classify_pages(extraction, {})

    assert results[0]["is_floor_plan"] is False
    assert "error" in results[0]


def test_classify_pages_returns_empty_list_for_no_pages():
    assert classify_pages({"pages": []}, {}) == []
