# Multi-page, multi-floor PDF support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a multi-page construction drawing set (only some pages are floor plans) produce one combined multi-floor GLB instead of failing with a Claude request-size error.

**Architecture:** A new cheap per-page classification pass (`classify_pages`) identifies which pages are floor plans and their floor index; `consumer.py`'s orchestration then calls the existing, unmodified `interpret_floor_plan` once per identified page instead of once for the whole document, merges the results into one `floors[]` list, and hands that to the existing, unmodified `reconstruct_3d`.

**Tech Stack:** Python, Anthropic Claude Vision API (`anthropic` SDK), pytest with `unittest.mock`.

**Spec:** `docs/superpowers/specs/2026-10-06-multi-page-multi-floor-design.md`

## Global Constraints

- `extractor.py` and `reconstructor.py` are not modified — both already support this (per-page extraction; multi-floor stacking by `index`/`height_m`).
- `interpret_floor_plan`'s own signature and internals are not modified — it is called once per identified floor-plan page instead of once per document.
- Floor ordering comes from Claude's own `floor_index` answer in classification, never from regex-parsing sheet titles or from page order.
- A per-page classification or interpretation failure is logged and skipped — it never fails the job. Only a *zero successful floors* outcome fails the job (same clear message as today's single-page "no geometry detected" case).
- No parallelization of Claude Vision calls — sequential, per the spec's explicit YAGNI call.

## Review Focus

- Zero floor-plan pages identified in a multi-page doc must fail the job with the existing clear "No floor-plan geometry detected in the PDF" message — not crash, not succeed with an empty GLB. Covered in Task 2's `test_run_pipeline_fails_when_no_floor_plan_pages_identified`.
- Two sheets both classified with the same `floor_index` (e.g. two pages both read as "Level 2") must not silently overwrite one another — the lower-confidence one is dropped into the skip list, not discarded without a trace. Covered in Task 2's `test_select_floor_plan_candidates_dedupes_by_floor_index`.
- A single classification API call raising (network error, rate limit, malformed JSON) must not abort the whole job — only that one page is treated as non-floor-plan. Covered in Task 1's `test_classify_pages_treats_call_failure_as_non_floor_plan`.
- A page classified as a floor plan whose full interpretation succeeds but returns an empty or missing `floors[]` list must be skipped, not crash on an index-0 lookup. Covered in Task 2's `test_run_pipeline_skips_candidate_with_empty_floors_list`.
- The existing single floor-plan-page case must still produce exactly the same final shape as before this change (no regression) — one page, classified as a floor plan, interprets and reconstructs to one floor. Covered in Task 2's `test_run_pipeline_single_floor_plan_page_still_works`.

---

## Task 1: Page classifier

**Files:**
- Create: `app/services/classifier.py`
- Test: `tests/test_classifier.py`

**Interfaces:**
- Produces: `classify_pages(extraction: dict, metadata: dict) -> list[dict]`, where each returned dict has keys `page_index` (int), `is_floor_plan` (bool), `floor_index` (int), `floor_label` (str), `confidence` (float), and optionally `error` (str, only present when the classification call itself failed).
- Consumes: `extraction["pages"]` — a list of dicts each with `page_index` (int) and `image_b64` (str), the exact shape `extractor.extract_pdf` already produces (see `app/services/extractor.py`). `settings.ANTHROPIC_API_KEY` from `app.core.config`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_classifier.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd pdf-twin-engine-ravindu/pdf-twin-engine && python -m pytest tests/test_classifier.py -v`
Expected: FAIL — `app.services.classifier` does not exist.

- [ ] **Step 3: Implement the classifier**

Create `app/services/classifier.py`:

```python
"""Cheap per-page triage: is this construction-drawing sheet a floor plan,
and if so which floor does it represent? See
docs/superpowers/specs/2026-10-06-multi-page-multi-floor-design.md.

Deliberately cheap and tolerant of failure: one bad page must never fail
the job, it just gets excluded from floor-plan consideration.
"""

import json
import logging
import re
from typing import Any, Dict, List

import anthropic

from app.core.config import settings

log = logging.getLogger("pdf-twin-engine")

_SCHEMA_HINT = (
    '{"is_floor_plan": bool, "floor_index": int, "floor_label": str, "confidence": float}'
)

_SYSTEM_PROMPT = (
    "You are triaging pages from an architectural construction drawing set. "
    "For the single page image provided, decide whether it is a FLOOR PLAN sheet "
    "(a top-down layout of rooms and walls for one level of a building) as opposed to "
    "a cover sheet, general notes, elevation, section, detail, schedule, or site plan. "
    "Return your answer STRICTLY as a single valid JSON object matching this schema:\n"
    f"{_SCHEMA_HINT}\n"
    "Rules:\n"
    "- No markdown fences, no prose, no extra keys.\n"
    "- If is_floor_plan is false, floor_index and floor_label are ignored but must still "
    "be present (use 0 and \"\").\n"
    "- floor_index: ground floor = 0, each level above = +1, basements = -1, -2, ... "
    "Base this on the sheet's own title/labeling (e.g. \"LEVEL 2\", \"2ND FLOOR\", \"L02\", "
    "\"GROUND FLOOR\", \"BASEMENT 1\"), not on page order.\n"
    "- floor_label: the sheet's own title text, for display only.\n"
    "- confidence: 0.0-1.0 for this classification."
)


def classify_pages(extraction: Dict[str, Any], metadata: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Runs one cheap Claude Vision call per page in extraction["pages"].

    Returns one dict per page, in page order. A page whose call raises or
    returns unparseable JSON is classified as non-floor-plan with an
    "error" key — this function never raises.
    """
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    results: List[Dict[str, Any]] = []

    for page in extraction.get("pages", []):
        page_index = page["page_index"]
        try:
            response = client.messages.create(
                model="claude-opus-4-5",
                max_tokens=256,
                system=_SYSTEM_PROMPT,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": page["image_b64"],
                            },
                        },
                        {"type": "text", "text": "Classify this page."},
                    ],
                }],
            )
            response_text = response.content[0].text.strip()
            clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", response_text, flags=re.DOTALL).strip()
            parsed = json.loads(clean)
            results.append({
                "page_index": page_index,
                "is_floor_plan": bool(parsed.get("is_floor_plan", False)),
                "floor_index": int(parsed.get("floor_index", 0)),
                "floor_label": str(parsed.get("floor_label", "")),
                "confidence": float(parsed.get("confidence", 0.0)),
            })
        except Exception as exc:  # noqa: BLE001 — one bad page must never fail the job
            log.warning("classification failed for page %s: %s", page_index, exc)
            results.append({
                "page_index": page_index,
                "is_floor_plan": False,
                "floor_index": 0,
                "floor_label": "",
                "confidence": 0.0,
                "error": str(exc),
            })

    return results
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd pdf-twin-engine-ravindu/pdf-twin-engine && python -m pytest tests/test_classifier.py -v`
Expected: PASS for all 5 tests.

- [ ] **Step 5: Commit**

```bash
cd pdf-twin-engine-ravindu/pdf-twin-engine
git add app/services/classifier.py tests/test_classifier.py
git commit -m "feat: add per-page floor-plan classification"
```

---

## Task 2: Multi-page orchestration in the consumer

**Files:**
- Modify: `app/worker/consumer.py`
- Modify: `tests/test_consumer.py`

**Interfaces:**
- Consumes: `classify_pages(extraction, metadata) -> list[dict]` (Task 1), the existing unmodified `interpret_floor_plan(extraction, metadata) -> dict`, the existing unmodified `reconstruct_3d(job_id, floor_schema) -> str`, the existing `_has_usable_wall_geometry(floor_schema) -> bool`.
- Produces: `_select_floor_plan_candidates(classifications: list[dict]) -> tuple[list[dict], list[dict]]` (ordered unique candidates, duplicate losers) — a new module-level function in `consumer.py`, directly unit-testable. `Consumer._run_pipeline`'s external behavior (still takes a `job: dict`, still calls `self.db.succeed_with_artifact`/`self.db.fail` the same way) is unchanged from the outside; only its internals change.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_consumer.py` (the existing file already has `_make_consumer()` and other fixtures — add these alongside them, using the same patterns):

```python
from app.worker.consumer import _select_floor_plan_candidates


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
```

Also add these orchestration-level tests (they call `_run_pipeline` directly, same style as the existing `test_run_pipeline_success_calls_succeed_with_artifact_and_cleans_up` test already in this file — reuse its pattern of patching `extract_pdf`, `boto3`, and now also `classify_pages`/`interpret_floor_plan`):

```python
import uuid
from unittest.mock import MagicMock, patch

from app.worker.consumer import Consumer


def _job():
    return {"id": 1, "job_uuid": str(uuid.uuid4()), "org_id": 1, "project_id": 1, "org_uuid": "o", "input": {"source_bucket": "b", "source_key": "k", "options": {"building_name": "Multi"}}}


def test_run_pipeline_fails_when_no_floor_plan_pages_identified(tmp_path):
    consumer = Consumer(sqs_client=MagicMock(), db=MagicMock())
    job = _job()

    with patch("app.worker.consumer.extract_pdf", return_value={"pages": [{"page_index": 0, "image_b64": "x", "width": 1, "height": 1}], "vector_geometry": None}), \
         patch("app.worker.consumer.classify_pages", return_value=[{"page_index": 0, "is_floor_plan": False, "floor_index": 0, "floor_label": "COVER", "confidence": 0.9}]), \
         patch.object(consumer, "_download_source"):
        try:
            consumer._run_pipeline(job)
            raise AssertionError("expected RuntimeError")
        except RuntimeError as exc:
            assert "No floor-plan geometry detected" in str(exc)


def test_run_pipeline_skips_candidate_with_empty_floors_list(tmp_path):
    consumer = Consumer(sqs_client=MagicMock(), db=MagicMock())
    job = _job()
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

    with patch("app.worker.consumer.extract_pdf", return_value=extraction), \
         patch("app.worker.consumer.classify_pages", return_value=classifications), \
         patch("app.worker.consumer.interpret_floor_plan", side_effect=[good_schema, empty_schema]), \
         patch("app.worker.consumer.reconstruct_3d", return_value=str(tmp_path / "model.glb")), \
         patch("app.worker.consumer.emit_artifact", return_value=("root", 10)), \
         patch.object(consumer, "_download_source"):
        (tmp_path / "model.glb").write_bytes(b"x")
        consumer._run_pipeline(job)

    args, kwargs = consumer.db.succeed_with_artifact.call_args
    assert kwargs["metadata"]["floors_reconstructed"] == 1
    assert kwargs["metadata"]["floors_identified"] == 2
    assert kwargs["metadata"]["floors_skipped"][0]["page_index"] == 1
    assert kwargs["metadata"]["floors_skipped"][0]["reason"] == "no_usable_geometry"


def test_run_pipeline_single_floor_plan_page_still_works(tmp_path):
    consumer = Consumer(sqs_client=MagicMock(), db=MagicMock())
    job = _job()
    extraction = {"pages": [{"page_index": 0, "image_b64": "x", "width": 1, "height": 1}], "vector_geometry": None}
    classifications = [{"page_index": 0, "is_floor_plan": True, "floor_index": 0, "floor_label": "L1", "confidence": 0.9}]
    schema = {"confidence": 0.9, "floors": [{"index": 0, "height_m": 3.0, "walls": [{"start": [0, 0], "end": [100, 0]}]}]}

    with patch("app.worker.consumer.extract_pdf", return_value=extraction), \
         patch("app.worker.consumer.classify_pages", return_value=classifications), \
         patch("app.worker.consumer.interpret_floor_plan", return_value=schema) as mock_interpret, \
         patch("app.worker.consumer.reconstruct_3d", return_value=str(tmp_path / "model.glb")) as mock_reconstruct, \
         patch("app.worker.consumer.emit_artifact", return_value=("root", 10)), \
         patch.object(consumer, "_download_source"):
        (tmp_path / "model.glb").write_bytes(b"x")
        consumer._run_pipeline(job)

    mock_interpret.assert_called_once()
    reconstruct_floor_schema = mock_reconstruct.call_args.args[1]
    assert len(reconstruct_floor_schema["floors"]) == 1
    assert reconstruct_floor_schema["floors"][0]["index"] == 0
    kwargs = consumer.db.succeed_with_artifact.call_args.kwargs
    assert kwargs["metadata"]["floors_reconstructed"] == 1
    assert kwargs["metadata"]["floors_skipped"] == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd pdf-twin-engine-ravindu/pdf-twin-engine && python -m pytest tests/test_consumer.py -v`
Expected: FAIL — `_select_floor_plan_candidates` and `classify_pages` import from `app.worker.consumer` don't exist yet; the new orchestration tests fail because `_run_pipeline` still makes a single whole-document `interpret_floor_plan` call and has no `floors_identified`/`floors_reconstructed`/`floors_skipped` metadata keys.

- [ ] **Step 3: Add the import and the candidate-selection helper**

In `app/worker/consumer.py`, add the import alongside the existing service imports:

```python
from app.services.classifier import classify_pages
```

Add this module-level function, placed after `_has_usable_wall_geometry`:

```python
def _select_floor_plan_candidates(classifications: list) -> tuple:
    """Filters to is_floor_plan candidates, deduped by floor_index (keeps
    the higher-confidence one), ordered by floor_index ascending.

    Returns (candidates, duplicate_losers) — duplicate_losers are the
    lower-confidence classifications that lost a floor_index collision,
    for the caller to record in the skip list rather than discard silently.
    """
    floor_plan_only = [c for c in classifications if c.get("is_floor_plan")]
    best_by_index = {}
    duplicates = []
    for c in floor_plan_only:
        idx = c["floor_index"]
        existing = best_by_index.get(idx)
        if existing is None:
            best_by_index[idx] = c
        elif c["confidence"] > existing["confidence"]:
            duplicates.append(existing)
            best_by_index[idx] = c
        else:
            duplicates.append(c)
    ordered = sorted(best_by_index.values(), key=lambda c: c["floor_index"])
    return ordered, duplicates
```

- [ ] **Step 4: Rewrite `_run_pipeline`'s interpretation stage**

In `app/worker/consumer.py`, replace this block:

```python
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
```

with:

```python
            self.db.progress(job["id"], 30, "classifying")
            classifications = classify_pages(extraction, metadata)
            candidates, duplicates = _select_floor_plan_candidates(classifications)

            if not candidates:
                raise RuntimeError("No floor-plan geometry detected in the PDF")

            pages_by_index = {p["page_index"]: p for p in extraction.get("pages", [])}
            vectors = extraction.get("vector_geometry") or []
            skipped = [
                {"page_index": d["page_index"], "floor_label": d["floor_label"], "reason": "duplicate_floor_index"}
                for d in duplicates
            ]
            combined_floors = []
            confidences = []

            self.db.progress(job["id"], 40, "interpreting")
            for candidate in candidates:
                page = pages_by_index[candidate["page_index"]]
                page_extraction = {
                    "total_pages": 1,
                    "pages": [page],
                    "vector_geometry": [v for v in vectors if v.get("page") == candidate["page_index"]] or None,
                    "metadata": metadata,
                }
                try:
                    page_schema = interpret_floor_plan(page_extraction, metadata)
                    page_floors = page_schema.get("floors") or []
                    if not page_floors or not _has_usable_wall_geometry(page_schema):
                        skipped.append({"page_index": candidate["page_index"], "floor_label": candidate["floor_label"], "reason": "no_usable_geometry"})
                        continue
                except Exception as exc:  # noqa: BLE001 — one bad floor must never fail the job
                    log.warning("interpretation failed for floor %s (page %s): %s", candidate["floor_index"], candidate["page_index"], exc)
                    skipped.append({"page_index": candidate["page_index"], "floor_label": candidate["floor_label"], "reason": "interpretation_failed"})
                    continue

                floor = dict(page_floors[0])
                floor["index"] = candidate["floor_index"]
                combined_floors.append(floor)
                confidences.append(page_schema.get("confidence", 0.0))

            if not combined_floors:
                raise RuntimeError("No floor-plan geometry detected in the PDF")

            floor_schema = {
                "confidence": min(confidences) if confidences else 0.0,
                "floors": combined_floors,
            }

            self.db.progress(job["id"], 65, "reconstructing")
            glb_path = Path(reconstruct_3d(job["job_uuid"], floor_schema))
```

Note: `anthropic.BadRequestError` handling moves from a single document-wide catch into the per-candidate broad `except Exception` above — this is intentional (Global Constraints: a per-page interpretation failure is skipped, not job-ending). This removes the only use of the `anthropic` name in this file, so also delete the now-unused `import anthropic` line from the top of `app/worker/consumer.py` in this same step.

- [ ] **Step 5: Extend the succeeded-job metadata**

In the same file, in the `self.db.succeed_with_artifact(...)` call inside `_run_pipeline`, replace:

```python
                    metadata={
                        "floors": len(floor_schema.get("floors", [])),
                        "confidence": floor_schema.get("confidence", 0),
                        "building_name": metadata["building_name"],
                        "address": metadata["address"],
                        "scale_meters": metadata["scale_meters"],
                    },
```

with:

```python
                    metadata={
                        "floors": len(floor_schema.get("floors", [])),
                        "confidence": floor_schema.get("confidence", 0),
                        "building_name": metadata["building_name"],
                        "address": metadata["address"],
                        "scale_meters": metadata["scale_meters"],
                        "floors_identified": len(candidates),
                        "floors_reconstructed": len(combined_floors),
                        "floors_skipped": skipped,
                    },
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd pdf-twin-engine-ravindu/pdf-twin-engine && python -m pytest tests/test_consumer.py tests/test_classifier.py -v`
Expected: PASS for every test in both files, including the pre-existing consumer tests from earlier tasks (they still pass because `_job()`-style fixtures and the dispatch-branch tests don't touch `_run_pipeline`'s internals directly, except the ones this task explicitly updated).

- [ ] **Step 7: Run the full suite**

Run: `cd pdf-twin-engine-ravindu/pdf-twin-engine && python -m pytest -v`
Expected: PASS, full suite green, no import errors, no leftover references to the old single-call interpretation path.

- [ ] **Step 8: Commit**

```bash
cd pdf-twin-engine-ravindu/pdf-twin-engine
git add app/worker/consumer.py tests/test_consumer.py
git commit -m "feat(worker): classify pages and merge multiple floors into one twin"
```

---

## Task 3: Manual verification against the real 68-sheet document

**Files:** none (verification only).

- [ ] **Step 1: Run the updated engine against the real construction-drawing.pdf**

This needs a live environment with a real `ANTHROPIC_API_KEY`, Postgres, S3, and SQS reachable (e.g. the AWS demo stack, already set up) — not a unit-test concern. Submit the same `construction-drawing.pdf` (68 pages) that previously failed with `413 request_too_large` as a `pdf_twin` job against a project, and poll its status.

Expected: the job progresses through `classifying` → `interpreting` → `reconstructing` → `succeeded` (not `413`, not a crash). Check the resulting artifact's `metadata.floors_identified`, `floors_reconstructed`, and `floors_skipped` to confirm the sheet count makes sense for this document, and that the resulting Scene/GLB has multiple floors stacked.

- [ ] **Step 2: Record the outcome**

Note the actual floor count found, how long the job took end-to-end (sequential per-page Claude calls on ~68 pages — expect this to take a few minutes, not seconds), and any sheets that landed in `floors_skipped` with their reasons. No code change — this closes out the plan.
