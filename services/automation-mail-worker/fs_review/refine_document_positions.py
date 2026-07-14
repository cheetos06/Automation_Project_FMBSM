from __future__ import annotations

import hashlib
import json
import math
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

import fitz

from copilot_extract import (
    _repair_mojibake,
    prompt_path,
    read_json,
    run_copilot_single_page_extraction,
    write_json,
)


AMOUNT_KINDS = {
    "prior_amount_match",
    "prior_amount_difference",
    "calculation_match",
    "calculation_difference",
}
POSITION_REFINEMENT_VERSION = 3


def _bbox(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 4:
        return None
    try:
        x1, y1, x2, y2 = (float(item) for item in value[:4])
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in (x1, y1, x2, y2)):
        return None
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    if not all(0 <= item <= 1 for item in (x1, y1, x2, y2)):
        return None
    return x1, y1, x2, y2


def _point(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    try:
        x, y = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(item) and 0 <= item <= 1 for item in (x, y)):
        return None
    return x, y


def _right_middle(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    return min(0.985, bbox[2] + 0.012), (bbox[1] + bbox[3]) / 2


def _candidate(
    *,
    candidate_id: str,
    page: int,
    role: str,
    label: Any,
    kind: Any,
    original_bbox: Any,
    target: dict[str, Any],
    bbox_field: str,
    comment: Any = "",
    evidence: Any = "",
) -> dict[str, Any]:
    return {
        "id": candidate_id,
        "page": page,
        "role": role,
        "label": _repair_mojibake(label),
        "kind": str(kind or ""),
        "review_comment": _repair_mojibake(comment),
        "review_evidence": _repair_mojibake(evidence),
        "original_bbox_norm": list(_bbox(original_bbox) or []),
        "_target": target,
        "_bbox_field": bbox_field,
    }


def _collect(comparisons: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    by_page: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for comparison_index, comparison in enumerate(comparisons):
        for item_index, item in enumerate(comparison.get("annotations", [])):
            if not isinstance(item, dict) or not item.get("page"):
                continue
            page = int(item["page"])
            kind = str(item.get("kind") or "")
            by_page[page].append(
                _candidate(
                    candidate_id=f"c{comparison_index}:annotation:{item_index}",
                    page=page,
                    role="amount_cell" if kind in AMOUNT_KINDS else "present_text",
                    label=item.get("label"),
                    kind=kind,
                    original_bbox=item.get("bbox_norm"),
                    target=item,
                    bbox_field="bbox_norm",
                    comment=item.get("comment"),
                    evidence=item.get("evidence"),
                )
            )

        for requirement_index, requirement in enumerate(
            comparison.get("evidence_requirements", [])
        ):
            if not isinstance(requirement, dict) or not requirement.get("page"):
                continue
            page = int(requirement["page"])
            by_page[page].append(
                _candidate(
                    candidate_id=f"c{comparison_index}:requirement:{requirement_index}",
                    page=page,
                    role="evidence_region",
                    label=requirement.get("table_title"),
                    kind="evidence_requirement",
                    original_bbox=requirement.get("bbox_norm"),
                    target=requirement,
                    bbox_field="bbox_norm",
                    comment=requirement.get("reason"),
                )
            )
            for item_index, item in enumerate(requirement.get("items", [])):
                if not isinstance(item, dict):
                    continue
                by_page[page].append(
                    _candidate(
                        candidate_id=(
                            f"c{comparison_index}:requirement:{requirement_index}:"
                            f"item:{item_index}"
                        ),
                        page=page,
                        role="amount_cell",
                        label=(
                            f"{_repair_mojibake(item.get('row_label'))} | "
                            f"{_repair_mojibake(item.get('column_label'))}"
                        ),
                        kind="evidence_item",
                        original_bbox=item.get("bbox_norm"),
                        target=item,
                        bbox_field="bbox_norm",
                    )
                )

        for item_index, item in enumerate(comparison.get("removed_prior_blocks", [])):
            if not isinstance(item, dict) or not item.get("page"):
                continue
            page = int(item["page"])
            by_page[page].append(
                _candidate(
                    candidate_id=f"c{comparison_index}:removed:{item_index}",
                    page=page,
                    role="missing_prior",
                    label=item.get("label"),
                    kind="removed_prior_block",
                    original_bbox=item.get("anchor_bbox_norm"),
                    target=item,
                    bbox_field="anchor_bbox_norm",
                    comment=item.get("comment"),
                )
            )
    return dict(by_page)


def _public(candidate: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in candidate.items() if not key.startswith("_")}


def _digest(page: int, candidates: list[dict[str, Any]]) -> str:
    payload = {
        "version": POSITION_REFINEMENT_VERSION,
        "page": page,
        "candidates": [_public(item) for item in candidates],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _inside(point: tuple[float, float], bbox: tuple[float, float, float, float]) -> bool:
    return bbox[0] <= point[0] <= bbox[2] and bbox[1] <= point[1] <= bbox[3]


def _safe_missing_anchor(
    point: tuple[float, float] | None,
    original_bbox: tuple[float, float, float, float] | None,
    occupied: list[tuple[float, float, float, float]],
) -> tuple[float, float]:
    if point is None:
        y = (original_bbox[1] + original_bbox[3]) / 2 if original_bbox else 0.5
        point = (0.95, y)
    if not any(_inside(point, box) for box in occupied):
        return min(0.985, max(0.015, point[0])), min(0.985, max(0.015, point[1]))

    overlapping = [box for box in occupied if box[1] <= point[1] <= box[3]]
    right = max((box[2] for box in overlapping), default=point[0]) + 0.015
    return min(0.985, max(0.015, right)), min(0.985, max(0.015, point[1]))


def _render_position_grid(pdf_path: Path, page_number: int, output_path: Path) -> None:
    source = fitz.open(pdf_path)
    overlay = fitz.open()
    try:
        source_page = source[page_number - 1]
        page = overlay.new_page(
            width=source_page.rect.width,
            height=source_page.rect.height,
        )
        page.show_pdf_page(page.rect, source, page_number - 1)
        grid_color = (0.72, 0.84, 0.96)
        label_color = (0.30, 0.48, 0.68)
        for step in range(1, 20):
            fraction = step / 20
            x = page.rect.width * fraction
            y = page.rect.height * fraction
            page.draw_line(
                (x, 0), (x, page.rect.height), color=grid_color, width=0.22
            )
            page.draw_line(
                (0, y), (page.rect.width, y), color=grid_color, width=0.22
            )
            page.insert_text(
                (x + 1, 6),
                f"{fraction:.2f}",
                fontsize=3.8,
                color=label_color,
            )
            page.insert_text(
                (1, y - 1),
                f"{fraction:.2f}",
                fontsize=3.8,
                color=label_color,
            )
        pixmap = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5), alpha=False)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        pixmap.save(output_path)
    finally:
        overlay.close()
        source.close()


def refine_document_positions(
    comparisons: list[dict[str, Any]],
    *,
    current_pdf: Path,
    extraction_dir: Path,
    runs_dir: Path,
    settings: dict[str, Any],
    skip_copilot: bool,
    only_pages: set[int] | None = None,
) -> dict[str, int]:
    candidates_by_page = _collect(comparisons)
    cache_dir = extraction_dir / "document_positions"
    cache_dir.mkdir(parents=True, exist_ok=True)
    page_count = 0
    candidate_count = 0

    for page, candidates in sorted(candidates_by_page.items()):
        if only_pages is not None and page not in only_pages:
            continue
        digest = _digest(page, candidates)
        cache_path = cache_dir / f"page_{page}.json"
        cached = read_json(cache_path) if cache_path.exists() else None
        if isinstance(cached, dict) and cached.get("input_digest") == digest:
            parsed = cached.get("result") or {}
        elif skip_copilot:
            raise FileNotFoundError(
                f"Missing current Copilot position refinement for page {page}: "
                f"{cache_path}"
            )
        else:
            context = (
                "The pale blue ruler grid is an extraction aid, not report "
                "content. Its lines and labels mark normalized coordinates at "
                "0.05 intervals. Use it to measure the exact visible bounds.\n\n"
                "Candidates for this page:\n"
                + json.dumps(
                    {"page": page, "candidates": [_public(item) for item in candidates]},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            tag = f"document_positions_page_{page}"
            print(f"[document-review] Copilot position refinement page {page}")
            with tempfile.TemporaryDirectory(prefix="stat_tick_position_") as temp_dir:
                position_image = Path(temp_dir) / f"page_{page}_grid.png"
                _render_position_grid(current_pdf, page, position_image)
                parsed = run_copilot_single_page_extraction(
                    position_image,
                    prompt_path(settings, "document_tick_positions_prompt"),
                    runs_dir.parent / "positions" / f"p{page}",
                    settings,
                    page=page,
                    tag=tag,
                    prompt_context=context,
                )
            write_json(
                cache_path,
                {"input_digest": digest, "page": page, "result": parsed},
            )

        placements = {
            str(item.get("id")): item
            for item in parsed.get("placements", [])
            if isinstance(item, dict) and item.get("id")
        }
        refined_boxes = [
            box
            for placement in placements.values()
            if (box := _bbox(placement.get("bbox_norm"))) is not None
        ]
        for candidate in candidates:
            placement = placements.get(candidate["id"], {})
            target = candidate["_target"]
            original = _bbox(candidate.get("original_bbox_norm"))
            if candidate["role"] == "missing_prior":
                anchor = _safe_missing_anchor(
                    _point(placement.get("anchor_norm")),
                    original,
                    refined_boxes,
                )
                target["tick_anchor_norm"] = list(anchor)
                continue

            refined = _bbox(placement.get("bbox_norm")) or original
            if refined is None:
                continue
            target[candidate["_bbox_field"]] = list(refined)
            target["tick_anchor_norm"] = list(_right_middle(refined))

        page_count += 1
        candidate_count += len(candidates)

    return {"position_pages": page_count, "position_candidates": candidate_count}
