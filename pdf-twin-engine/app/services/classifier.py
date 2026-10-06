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
