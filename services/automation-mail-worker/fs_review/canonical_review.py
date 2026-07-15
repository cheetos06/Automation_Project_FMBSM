from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from copilot_extract import prompt_path, run_copilot_prompt_extraction, write_json
from copilot_pool import PoolRun, run_account_pool


def _pages(document: dict[str, Any] | None) -> dict[int, dict[str, Any]]:
    return {
        int(page["page"]): page
        for page in (document or {}).get("pages", [])
        if isinstance(page, dict) and page.get("page") is not None
    }


def _page_view(page: dict[str, Any]) -> dict[str, Any]:
    return {
        "page": page.get("page"),
        "scope": page.get("scope"),
        "page_role": page.get("page_role"),
        "primary_title": page.get("primary_title"),
        "headings": page.get("headings", []),
        "statement_kind": page.get("statement_kind"),
        "entity": page.get("entity"),
        "period_end": page.get("period_end"),
        "blocks": [
            block for block in page.get("blocks", []) if block.get("reviewable", True)
        ],
        "tables": page.get("tables", []),
    }


def build_review_jobs(
    groups: list[dict[str, Any]],
    current: dict[str, Any],
    prior: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    current_pages = _pages(current)
    prior_pages = _pages(prior)
    jobs = []
    for index, group in enumerate(groups, start=1):
        current_numbers = [int(value) for value in group["current_pages"]]
        prior_numbers = [int(value) for value in group.get("prior_pages", [])]
        jobs.append(
            {
                "job_id": f"review_{index:03d}",
                "current_pages": current_numbers,
                "prior_pages": prior_numbers,
                "scope": group.get("scope", "other"),
                "current_records": [
                    _page_view(current_pages[page])
                    for page in current_numbers
                    if page in current_pages
                ],
                "prior_records": [
                    _page_view(prior_pages[page])
                    for page in prior_numbers
                    if page in prior_pages
                ],
            }
        )
    return jobs


def _batches(
    jobs: list[dict[str, Any]], *, max_jobs: int, max_chars: int
) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for job in jobs:
        size = len(json.dumps(job, ensure_ascii=False, separators=(",", ":")))
        if current and (len(current) >= max_jobs or current_chars + size > max_chars):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(job)
        current_chars += size
    if current:
        batches.append(current)
    return batches


def _comparison_items(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    items = parsed.get("comparisons")
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]
    if parsed.get("job_id"):
        return [parsed]
    return []


def _element_index(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for page in document.get("pages", []):
        page_number = int(page["page"])
        for block in page.get("blocks", []):
            if block.get("id"):
                result[str(block["id"])] = {
                    **block,
                    "page": page_number,
                    "element_type": "block",
                }
        for table in page.get("tables", []):
            if table.get("id"):
                result[str(table["id"])] = {
                    **table,
                    "page": page_number,
                    "element_type": "table",
                }
            for row in table.get("rows", []):
                for cell in row.get("cells", []):
                    if cell.get("id"):
                        result[str(cell["id"])] = {
                            **cell,
                            "page": page_number,
                            "row_label": row.get("label"),
                            "element_type": "table_cell",
                        }
        for line in page.get("fs_lines", []):
            for cell in line.get("cells", []):
                if cell.get("id"):
                    result[str(cell["id"])] = {
                        **cell,
                        "page": page_number,
                        "row_label": line.get("libelle"),
                        "element_type": "fs_cell",
                    }
    return result


def _bbox(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 4:
        return None
    try:
        x1, y1, x2, y2 = (float(value[index]) for index in range(4))
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) and 0 <= value <= 1 for value in (x1, y1, x2, y2)):
        return None
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    return x1, y1, x2, y2


def _right_middle(box: tuple[float, float, float, float]) -> list[float]:
    return [min(0.985, box[2] + 0.012), (box[1] + box[3]) / 2]


def _inside(point: tuple[float, float], box: tuple[float, float, float, float]) -> bool:
    return box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]


def _missing_anchor(
    item: dict[str, Any],
    *,
    default_page: int,
    elements: dict[str, dict[str, Any]],
    page_boxes: dict[int, list[tuple[float, float, float, float]]],
    reserved: dict[int, list[tuple[float, float]]],
) -> tuple[int, list[float]]:
    after = elements.get(str(item.get("after_current_id") or ""))
    before = elements.get(str(item.get("before_current_id") or ""))
    page = int(
        (after or {}).get("page")
        or (before or {}).get("page")
        or item.get("page")
        or default_page
    )
    after_box = _bbox((after or {}).get("bbox_norm"))
    before_box = _bbox((before or {}).get("bbox_norm"))
    if after_box and before_box and int(after["page"]) == int(before["page"]):
        y = (after_box[3] + before_box[1]) / 2
        x = min(0.97, max(after_box[2], before_box[2]) + 0.018)
    elif after_box:
        y = min(0.965, after_box[3] + 0.018)
        x = min(0.97, after_box[2] + 0.018)
    elif before_box:
        y = max(0.035, before_box[1] - 0.018)
        x = min(0.97, before_box[2] + 0.018)
    else:
        x, y = 0.95, 0.5

    boxes = page_boxes.get(page, [])
    candidate = (x, y)
    occupied = reserved.setdefault(page, [])
    collision = any(_inside(candidate, box) for box in boxes) or any(
        abs(candidate[0] - point[0]) < 0.025 and abs(candidate[1] - point[1]) < 0.025
        for point in occupied
    )
    if collision:
        for offset in (
            0.04,
            -0.04,
            0.08,
            -0.08,
            0.12,
            -0.12,
            0.16,
            -0.16,
            0.20,
            -0.20,
        ):
            trial = (min(0.965, max(0.04, x)), min(0.97, max(0.03, y + offset)))
            if not any(_inside(trial, box) for box in boxes) and not any(
                abs(trial[0] - point[0]) < 0.025
                and abs(trial[1] - point[1]) < 0.025
                for point in occupied
            ):
                candidate = trial
                break
        else:
            candidate = (0.985, min(0.97, max(0.03, y)))
    occupied.append(candidate)
    return page, [round(candidate[0], 5), round(candidate[1], 5)]


def resolve_comparison_targets(
    comparison: dict[str, Any],
    *,
    job: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    elements = _element_index(current)
    page_boxes: dict[int, list[tuple[float, float, float, float]]] = {}
    for element in elements.values():
        box = _bbox(element.get("bbox_norm"))
        if box:
            page_boxes.setdefault(int(element["page"]), []).append(box)

    quality_notes = list(comparison.get("quality_notes", []))
    annotations = []
    for item in comparison.get("annotations", []):
        if not isinstance(item, dict):
            continue
        element = elements.get(str(item.get("target_id") or ""))
        box = _bbox((element or {}).get("bbox_norm"))
        if element is None or box is None:
            quality_notes.append(
                f"Ignored annotation with unresolved target_id {item.get('target_id')!r}."
            )
            continue
        resolved = dict(item)
        resolved["page"] = int(element["page"])
        resolved["bbox_norm"] = list(box)
        resolved["tick_anchor_norm"] = _right_middle(box)
        annotations.append(resolved)

    requirements = []
    for requirement in comparison.get("evidence_requirements", []):
        if not isinstance(requirement, dict):
            continue
        table = elements.get(str(requirement.get("table_id") or ""))
        table_box = _bbox((table or {}).get("bbox_norm"))
        if table is None or table_box is None:
            quality_notes.append(
                f"Ignored evidence requirement with unresolved table_id {requirement.get('table_id')!r}."
            )
            continue
        resolved_requirement = dict(requirement)
        resolved_requirement["page"] = int(table["page"])
        resolved_requirement["bbox_norm"] = list(table_box)
        resolved_requirement["tick_anchor_norm"] = _right_middle(table_box)
        items = []
        for item in requirement.get("items", []):
            if not isinstance(item, dict):
                continue
            cell = elements.get(str(item.get("target_id") or ""))
            cell_box = _bbox((cell or {}).get("bbox_norm"))
            if cell is None or cell_box is None:
                continue
            resolved_item = dict(item)
            resolved_item["bbox_norm"] = list(cell_box)
            resolved_item["tick_anchor_norm"] = _right_middle(cell_box)
            if resolved_item.get("amount") is None:
                resolved_item["amount"] = cell.get("amount")
            if not resolved_item.get("row_label"):
                resolved_item["row_label"] = cell.get("row_label")
            items.append(resolved_item)
        resolved_requirement["items"] = items
        requirements.append(resolved_requirement)

    removed = []
    reserved_missing: dict[int, list[tuple[float, float]]] = {}
    for item in annotations:
        anchor = item.get("tick_anchor_norm")
        if isinstance(anchor, (list, tuple)) and len(anchor) >= 2:
            reserved_missing.setdefault(int(item["page"]), []).append(
                (float(anchor[0]), float(anchor[1]))
            )
    for requirement in requirements:
        anchor = requirement.get("tick_anchor_norm")
        if isinstance(anchor, (list, tuple)) and len(anchor) >= 2:
            reserved_missing.setdefault(int(requirement["page"]), []).append(
                (float(anchor[0]), float(anchor[1]))
            )
    default_page = int(job["current_pages"][0])
    for item in comparison.get("removed_prior_blocks", []):
        if not isinstance(item, dict):
            continue
        page, anchor = _missing_anchor(
            item,
            default_page=default_page,
            elements=elements,
            page_boxes=page_boxes,
            reserved=reserved_missing,
        )
        resolved = dict(item)
        resolved["page"] = page
        resolved["anchor_bbox_norm"] = [
            max(0.0, anchor[0] - 0.002),
            max(0.0, anchor[1] - 0.002),
            min(1.0, anchor[0] + 0.002),
            min(1.0, anchor[1] + 0.002),
        ]
        resolved["tick_anchor_norm"] = anchor
        removed.append(resolved)

    return {
        "job_id": job["job_id"],
        "current_pages": job["current_pages"],
        "prior_pages": job["prior_pages"],
        "scope": comparison.get("scope") or job.get("scope") or "other",
        "annotations": annotations,
        "evidence_requirements": requirements,
        "removed_prior_blocks": removed,
        "quality_notes": quality_notes,
        "canonical_targets": True,
    }


def run_structured_review(
    jobs: list[dict[str, Any]],
    *,
    current: dict[str, Any],
    comparisons_dir: Path,
    runs_dir: Path,
    settings: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    max_jobs = max(1, int(settings.get("semantic_batch_groups", 3)))
    max_chars = max(10000, int(settings.get("semantic_batch_max_chars", 60000)))
    batches = _batches(jobs, max_jobs=max_jobs, max_chars=max_chars)
    prompt = prompt_path(settings, "canonical_compare_prompt")

    def operation(
        batch_job: dict[str, Any], account_settings: dict[str, Any], account_name: str
    ) -> dict[str, Any]:
        batch_number = int(batch_job["batch"])
        batch_items = batch_job["jobs"]
        print(
            f"[document-review] structured batch {batch_number} "
            f"({len(batch_items)} jobs) via Copilot account {account_name}"
        )
        parsed = run_copilot_prompt_extraction(
            prompt,
            runs_dir / f"batch_{batch_number:03d}",
            account_settings,
            tag=f"canonical_review_batch_{batch_number:03d}",
            prompt_context=(
                "Structured review jobs:\n"
                + json.dumps(batch_items, ensure_ascii=False, separators=(",", ":"))
            ),
        )
        return {"batch": batch_number, "parsed": parsed}

    pool_jobs = [
        {"batch": index, "jobs": batch}
        for index, batch in enumerate(batches, start=1)
    ]
    pool: PoolRun[dict[str, Any]] = run_account_pool(pool_jobs, settings, operation)
    returned: dict[str, dict[str, Any]] = {}
    for result in pool.results:
        for item in _comparison_items(result["parsed"]):
            if item.get("job_id"):
                returned[str(item["job_id"])] = item

    missing = [job for job in jobs if job["job_id"] not in returned]
    retry_stats: dict[str, Any] = {"calls": 0, "elapsed_seconds": 0.0, "accounts": {}}
    if missing:
        print(
            f"[document-review] retrying {len(missing)} omitted structured job(s) individually"
        )
        retry_jobs = [
            {"batch": len(batches) + index, "jobs": [job]}
            for index, job in enumerate(missing, start=1)
        ]
        retry_pool: PoolRun[dict[str, Any]] = run_account_pool(
            retry_jobs, settings, operation
        )
        retry_stats = {
            "calls": len(retry_jobs),
            "elapsed_seconds": round(retry_pool.elapsed_seconds, 3),
            "accounts": retry_pool.account_stats,
        }
        for result in retry_pool.results:
            for item in _comparison_items(result["parsed"]):
                if item.get("job_id"):
                    returned[str(item["job_id"])] = item

    unresolved = [job["job_id"] for job in jobs if job["job_id"] not in returned]
    if unresolved:
        raise RuntimeError(f"Copilot omitted structured review jobs after retry: {unresolved}")

    comparisons = []
    comparisons_dir.mkdir(parents=True, exist_ok=True)
    for job in jobs:
        resolved = resolve_comparison_targets(
            returned[job["job_id"]], job=job, current=current
        )
        write_json(comparisons_dir / f"{job['job_id']}.json", resolved)
        comparisons.append(resolved)

    stats = {
        "semantic_calls": len(pool_jobs) + int(retry_stats["calls"]),
        "semantic_batches": len(pool_jobs),
        "elapsed_seconds": round(
            pool.elapsed_seconds + float(retry_stats["elapsed_seconds"]), 3
        ),
        "accounts": pool.account_stats,
        "retry": retry_stats,
    }
    return comparisons, stats
