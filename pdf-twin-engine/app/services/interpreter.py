import json
import re
import anthropic
from typing import Dict, Any

from app.core.config import settings

# Expected JSON schema returned by Claude
_SCHEMA_HINT = (
    '{"confidence": float, "floors": [{'
    '"index": int, "height_m": float, '
    '"rooms": [{"name": str, "area_m2": float}], '
    '"walls": [{"start": [x, y], "end": [x, y]}]'
    "}]}"
)

_SYSTEM_PROMPT = (
    "You are an expert architectural engineering assistant. "
    "Analyse the provided floor plan images and extract all structural elements. "
    "Return your answer STRICTLY as a single valid JSON object matching this schema:\n"
    f"{_SCHEMA_HINT}\n"
    "Rules:\n"
    "- No markdown fences, no prose, no extra keys.\n"
    "- Wall coordinates must be in normalised units (0-1000 grid).\n"
    "- If a floor cannot be determined, use index 0.\n"
    "- Assign a confidence score (0.0-1.0) for the overall extraction quality.\n"
    "- Use the supplied building metadata for context where helpful."
)


def interpret_floor_plan(extraction_data: Dict[str, Any], metadata: Dict[str, Any]) -> Dict[str, Any]:
    """
    Analyses floor plan images using Claude Vision and returns a structured 2D geometry schema.

    Args:
        extraction_data: Output from extractor.py (base64 page images + optional vector geometry).
        metadata:        Building metadata for architectural context.

    Returns:
        Structured floor schema dict with floors, rooms, walls, and confidence score.

    Raises:
        RuntimeError: If Claude response cannot be parsed into valid JSON after retries.
    """
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    # Build message content - images first, then text prompt
    message_content = []

    for page in extraction_data.get("pages", []):
        message_content.append({
            "type": "image",
            "source": {
                "type":       "base64",
                "media_type": "image/jpeg",
                "data":       page["image_b64"],
            },
        })

    # Append vector geometry hint if available (helps Claude with CAD plans)
    vector_hint = ""
    if extraction_data.get("vector_geometry"):
        vector_count = len(extraction_data["vector_geometry"])
        vector_hint  = f"\nNote: {vector_count} vector primitives were also detected in this PDF (CAD export)."

    message_content.append({
        "type": "text",
        "text": (
            f"Building metadata: {json.dumps(metadata)}"
            f"{vector_hint}\n"
            "Return only the JSON output."
        ),
    })

    # Call Claude Vision - retry once on JSON parse failure
    for attempt in range(2):
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=4096,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": message_content}],
        )

        response_text = response.content[0].text.strip()

        try:
            # Strip accidental markdown fences if Claude adds them
            clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", response_text, flags=re.DOTALL).strip()
            structured_data = json.loads(clean)
            return structured_data

        except json.JSONDecodeError:
            if attempt == 1:
                raise RuntimeError(
                    f"Failed to parse Claude response into valid JSON after 2 attempts.\n"
                    f"Raw output: {response_text}"
                )
            # On first failure, append correction instruction and retry
            message_content.append({"role": "assistant", "content": response_text})
            message_content.append({
                "role": "user",
                "content": "Your previous response was not valid JSON. Return only the JSON object, nothing else."
            })