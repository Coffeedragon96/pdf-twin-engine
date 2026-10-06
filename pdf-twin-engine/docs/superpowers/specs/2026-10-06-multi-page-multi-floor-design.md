# Multi-page, multi-floor PDF support — Design

Approved 2026-10-06. Extends the pdf-twin-engine to handle real
construction drawing sets (tens of pages: cover sheet, notes, elevations,
sections, details, schedules, and — among them — one or more floor-plan
sheets), producing a single combined multi-floor 3D twin instead of
failing outright.

## Goal

A PDF with many pages, only some of which are floor plans, produces one
GLB containing every identified floor stacked in the correct vertical
order — the same combined-twin shape `reconstruct_3d` already supports
for a single document with multiple `floors[]` entries, just sourced from
multiple pages instead of one.

## Non-goals

- No change to `extractor.py` or `reconstructor.py` — both already support
  this (per-page image extraction; multi-floor stacking by `index`/
  `height_m`). This spec only changes how pages get interpreted and
  merged before reaching `reconstruct_3d`.
- No parallelization of Claude Vision calls — sequential for now (YAGNI).
  Revisit only if job latency becomes a real problem.
- No regex-based sheet-title parsing — floor ordering comes from asking
  Claude directly for a `floor_index`, not from pattern-matching titles
  like "LEVEL 2" / "2ND FLOOR PLAN" / "L02".
- No change to the `pdf_twin` job contract, artifact shape, or anything
  in x2-backend — this is entirely internal to this engine's pipeline.

## Architecture

```
extract_pdf (unchanged) → one image per page
  → classify_pages (NEW, app/services/classifier.py): one cheap Claude
    Vision call per page → {is_floor_plan, floor_index, floor_label, confidence}
  → consumer.py orchestration: for each page classified as a floor plan,
    call interpret_floor_plan (UNCHANGED signature) on that single page's
    extraction
  → merge (in consumer.py): combine each page's floor result into one
    floors[] list using classification's floor_index; skip/log pages that
    fail interpretation or have no usable wall geometry (reuses the
    existing no-geometry check)
  → reconstruct_3d (unchanged) ← one combined multi-floor floor_schema
  → emit/succeed (unchanged), metadata extended with per-floor outcome
```

## Components

### 1. `app/services/classifier.py` (new file)

`classify_pages(extraction: dict, metadata: dict) -> list[dict]` — one
Claude Vision call per page in `extraction["pages"]`. Deliberately cheap:
small `max_tokens`, no JSON-retry logic (a classification failure for one
page just drops that page from consideration, it never fails the job).

Request: a single page image + a short system prompt asking for strict
JSON:
```json
{"is_floor_plan": bool, "floor_index": int, "floor_label": str, "confidence": float}
```
Prompt instructs: ground floor = 0, each level above = +1, basements
negative, and `floor_label` is the sheet's own title text for display
only (never parsed for ordering — `floor_index` is authoritative).
Non-floor-plan sheets (cover, notes, elevations, sections, details,
schedules, site plans) get `is_floor_plan: false`.

Returns one classification dict per page, in page order, each tagged
with its `page_index`. A page whose classification call raises or returns
unparseable JSON gets `is_floor_plan: false` with a `"error"` note — same
"skip, don't fail" treatment as a low-confidence floor-plan call.

### 2. `consumer.py` — `_run_pipeline` rewrite

Replaces the current single `interpret_floor_plan(extraction, metadata)`
call over the whole document with:

1. `extract_pdf` — unchanged, still extracts every page.
2. `classify_pages(extraction, metadata)` — new step.
3. Filter to `is_floor_plan: true` pages. Zero such pages → raise the same
   "No floor-plan geometry detected in the PDF" error the single-page path
   already raises (Critical-1 behavior preserved, now fed by zero
   *candidates* instead of zero *walls*).
4. For each candidate page (in `floor_index` order, ties broken by
   `confidence` descending): build a single-page extraction dict (same
   shape `extractor.py` already produces, just `"pages": [that one page]`,
   `"vector_geometry"` filtered to that page's entries), call the
   existing unmodified `interpret_floor_plan` on it, apply the existing
   `_has_usable_wall_geometry` check. Success → take the first entry of
   the returned `floors[]`, override its `"index"` with the
   classification's `floor_index`, append to the combined list. Any
   failure (interpretation error, no usable geometry) → log and add to a
   `skipped` list with a reason, continue to the next candidate — never
   abort the job for one bad floor.
5. Two candidates mapping to the same `floor_index` → keep the
   higher-`confidence` one, the other goes to `skipped` as
   `"duplicate_floor_index"`.
6. Combined list empty after step 4-5 → same zero-floors failure as step 3.
7. Build one `floor_schema = {"confidence": <min of per-floor
   confidences>, "floors": <combined list>}`, pass to `reconstruct_3d`
   unchanged.
8. `succeed_with_artifact`'s `metadata` gains `floors_identified` (count
   from step 3), `floors_reconstructed` (count from step 7),
   `floors_skipped` (list of `{page_index, floor_label, reason}` from
   steps 4-6) — purely additive to the existing metadata shape.

### 3. Unchanged

`extractor.py`, `reconstructor.py`, `interpret_floor_plan`'s own
signature and internals, the SQS/DB contract, artifact shape
(`kind="glb"`), heartbeat/visibility handling.

## Error handling

- Per-page classification or interpretation failure: logged, added to
  `skipped`, never aborts the job (explicit design choice — "succeed with
  the floors that worked").
- Zero floor-plan pages identified, or zero of the identified pages
  reconstruct successfully: job fails with a clear message, exactly like
  today's single-page "no geometry detected" case — never a partial/empty
  GLB treated as success.
- Anthropic `BadRequestError` handling already added to `consumer.py` for
  the single `interpret_floor_plan` call extends naturally to each
  per-page call in the loop — same catch, same rewritten message, just
  triggered per-candidate instead of once per document.

## Testing

- `classify_pages`: mocked Claude responses — a floor-plan page, a
  non-floor-plan page, a page whose call raises (treated as
  non-floor-plan), a page whose JSON is malformed (same).
- Merge logic in `consumer.py`: multiple floors combine correctly ordered
  by `floor_index`; a failing candidate is skipped and logged, others
  still succeed; duplicate `floor_index` keeps the higher-confidence one;
  zero floor-plan pages and zero-successful-floors both fail the job with
  the existing clear message.
- Updated `_run_pipeline` orchestration test covering a 3-page synthetic
  document: one non-floor-plan page, two floor-plan pages (different
  `floor_index`) — asserts `reconstruct_3d` receives a `floors[]` of
  length 2 in the right order, and `succeed_with_artifact`'s metadata
  carries the new fields.
- Existing single-page tests remain valid unchanged: a 1-page PDF still
  flows through classify (1 page, floor-plan) → interpret → merge (1
  floor) → reconstruct, same as before.

## Open items deferred

- Sequential Claude Vision calls (classification + interpretation) mean a
  68-page document makes ~70+ API calls per job — noticeably slower than
  today's single call. Parallelizing classification calls (they're
  independent) is the first optimization to reach for if this becomes a
  real problem; not built now.
- No cost/rate-limit guard on page count — a pathological 1000-page PDF
  would make 1000+ calls. Not addressed here; worth a sane upper bound on
  page count if this becomes a real input.
