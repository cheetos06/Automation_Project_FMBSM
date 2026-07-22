from __future__ import annotations

import math
import json
import re
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any

import fitz

from copilot_extract import (
    _repair_mojibake,
    merge_layouts,
    normalize_layout,
    prompt_path,
    run_copilot_single_page_extraction,
    write_json,
)
from copilot_pool import PoolRun, run_account_pool
from pixel_geometry import refine_canonical_document
from visual_geometry import render_page_grid


VALID_SCOPES = {"annual", "consolidated", "auditor_report", "tax", "other"}
VALID_ROLES = {
    "cover",
    "contents",
    "primary_statement",
    "accounting_policies",
    "narrative_note",
    "annex_table",
    "auditor_report",
    "tax_form",
    "blank",
    "other",
}
VALID_BLOCK_KINDS = {
    "title",
    "heading",
    "paragraph",
    "date",
    "entity",
    "table_title",
    "reference_note",
    "other",
}
FS_FIELDS = {"brut", "amortissement", "montant_n", "montant_n_1"}


def _fold(value: Any) -> str:
    text = unicodedata.normalize("NFKD", _text(value))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip()


def _page_count(pdf: Path) -> int:
    document = fitz.open(pdf)
    try:
        return document.page_count
    finally:
        document.close()


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    text = str(value).strip()
    negative = text.startswith("(") and text.endswith(")")
    for old, new in (
        ("\u00a0", ""),
        (" ", ""),
        ("EUR", ""),
        ("€", ""),
        (",", "."),
        ("(", ""),
        (")", ""),
    ):
        text = text.replace(old, new)
    try:
        number = float(text)
    except ValueError:
        return None
    if negative:
        number = -abs(number)
    return number if math.isfinite(number) else None


def _bbox(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 4:
        return None
    try:
        x1, y1, x2, y2 = (float(value[index]) for index in range(4))
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
    if x2 - x1 < 0.001 or y2 - y1 < 0.001:
        return None
    return [round(x1, 5), round(y1, 5), round(x2, 5), round(y2, 5)]


def _text(value: Any) -> str:
    return _repair_mojibake(value).strip()


def _global_id(period_role: str, page: int, raw: Any, fallback: str) -> str:
    local = _text(raw) or fallback
    return f"{period_role}:p{page}:{local}"


def _normalize_page(
    parsed: dict[str, Any], *, page: int, period_role: str, source: Path
) -> dict[str, Any]:
    scope = _text(parsed.get("scope")).lower().replace(" ", "_")
    role = _text(parsed.get("page_role")).lower().replace(" ", "_")
    if scope not in VALID_SCOPES:
        scope = "other"
    if role not in VALID_ROLES:
        role = "other"

    raw_blocks = parsed.get("blocks") if isinstance(parsed.get("blocks"), list) else []
    blocks: list[dict[str, Any]] = []
    block_ids: dict[str, str] = {}
    for index, item in enumerate(raw_blocks, start=1):
        if not isinstance(item, dict):
            continue
        content = _text(item.get("text"))
        box = _bbox(item.get("bbox_norm"))
        if not content or box is None:
            continue
        raw_id = _text(item.get("id")) or f"b{index}"
        element_id = _global_id(period_role, page, raw_id, f"b{index}")
        block_ids[raw_id] = element_id
        kind = _text(item.get("kind")).lower().replace(" ", "_")
        if kind not in VALID_BLOCK_KINDS:
            kind = "other"
        reviewable = bool(item.get("reviewable", True)) and kind != "reference_note"
        blocks.append(
            {
                "id": element_id,
                "kind": kind,
                "text": content,
                "bbox_norm": box,
                "parent_id": _text(item.get("parent_id")) or None,
                "reviewable": reviewable,
            }
        )
    for block in blocks:
        parent_id = block.get("parent_id")
        block["parent_id"] = block_ids.get(str(parent_id)) if parent_id else None

    raw_lines = parsed.get("fs_lines") if isinstance(parsed.get("fs_lines"), list) else []
    fs_lines: list[dict[str, Any]] = []
    fs_labels: set[str] = set()
    for line_index, item in enumerate(raw_lines, start=1):
        if not isinstance(item, dict):
            continue
        label = _text(item.get("libelle") or item.get("label"))
        if not label:
            continue
        raw_line_id = _text(item.get("id")) or f"fs{line_index}"
        line_id = _global_id(period_role, page, raw_line_id, f"fs{line_index}")
        cells: list[dict[str, Any]] = []
        raw_cells = item.get("cells") if isinstance(item.get("cells"), list) else []
        for cell_index, cell in enumerate(raw_cells, start=1):
            if not isinstance(cell, dict):
                continue
            field = _text(cell.get("field")).lower()
            box = _bbox(cell.get("bbox_norm"))
            amount = _number(cell.get("amount"))
            if field not in FS_FIELDS or amount is None or box is None:
                continue
            cell_id = _global_id(
                period_role,
                page,
                cell.get("id"),
                f"{raw_line_id}c{cell_index}",
            )
            cells.append(
                {
                    "id": cell_id,
                    "field": field,
                    "display_column": _text(cell.get("display_column")),
                    "amount": amount,
                    "bbox_norm": box,
                }
            )
        fs_lines.append(
            {
                "id": line_id,
                "statement": _text(item.get("statement")),
                "libelle": label,
                "label_bbox_norm": _bbox(item.get("label_bbox_norm")),
                "is_total": bool(item.get("is_total", False)),
                "cells": cells,
            }
        )
        fs_labels.add(" ".join(label.lower().split()))

    # Individual primary-statement account rows are handled by the mapper,
    # even if the model also returned their labels as generic text blocks.
    if role == "primary_statement" and fs_labels:
        for block in blocks:
            if " ".join(block["text"].lower().split()) in fs_labels:
                block["reviewable"] = False

    raw_tables = parsed.get("tables") if isinstance(parsed.get("tables"), list) else []
    tables: list[dict[str, Any]] = []
    for table_index, item in enumerate(raw_tables, start=1):
        if not isinstance(item, dict):
            continue
        raw_table_id = _text(item.get("id")) or f"t{table_index}"
        table_id = _global_id(period_role, page, raw_table_id, f"t{table_index}")
        rows: list[dict[str, Any]] = []
        raw_rows = item.get("rows") if isinstance(item.get("rows"), list) else []
        for row_index, row in enumerate(raw_rows, start=1):
            if not isinstance(row, dict):
                continue
            cells: list[dict[str, Any]] = []
            raw_cells = row.get("cells") if isinstance(row.get("cells"), list) else []
            for cell_index, cell in enumerate(raw_cells, start=1):
                if not isinstance(cell, dict):
                    continue
                box = _bbox(cell.get("bbox_norm"))
                if box is None:
                    continue
                cells.append(
                    {
                        "id": _global_id(
                            period_role,
                            page,
                            cell.get("id"),
                            f"{raw_table_id}r{row_index}c{cell_index}",
                        ),
                        "column_label": _text(cell.get("column_label")),
                        "text": _text(cell.get("text")) or None,
                        "amount": _number(cell.get("amount")),
                        "bbox_norm": box,
                        "support_type": _text(cell.get("support_type")) or "other",
                    }
                )
            rows.append({"label": _text(row.get("label")), "cells": cells})
        tables.append(
            {
                "id": table_id,
                "title": _text(item.get("title")),
                "bbox_norm": _bbox(item.get("bbox_norm")),
                "rows": rows,
            }
        )

    headings = parsed.get("headings") if isinstance(parsed.get("headings"), list) else []
    return {
        "page": page,
        "period_role": period_role,
        "scope": scope,
        "statement_set_id": _text(parsed.get("statement_set_id")) or None,
        "entity": _text(parsed.get("entity")) or None,
        "period_end": parsed.get("period_end"),
        "page_role": role,
        "primary_title": _text(parsed.get("primary_title")) or None,
        "headings": [_text(value) for value in headings if _text(value)],
        "statement_kind": _text(parsed.get("statement_kind")) or None,
        "reviewable": bool(parsed.get("reviewable", True)),
        "blocks": blocks,
        "fs_lines": fs_lines,
        "tables": tables,
        "quality_notes": [
            _text(value)
            for value in parsed.get("quality_notes", [])
            if _text(value)
        ]
        if isinstance(parsed.get("quality_notes"), list)
        else [],
        "source": str(source),
    }


def _move_fs_lines_to_table(
    page: dict[str, Any],
    *,
    role: str,
    scope: str | None,
    suffix: str,
    note: str,
) -> None:
    fs_lines = [line for line in page.get("fs_lines", []) if isinstance(line, dict)]
    boxes = [
        box
        for line in fs_lines
        for box in [
            _bbox(line.get("label_bbox_norm")),
            *[_bbox(cell.get("bbox_norm")) for cell in line.get("cells", [])],
        ]
        if box is not None
    ]
    table_box = (
        [
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        ]
        if boxes
        else None
    )
    rows = []
    for line in fs_lines:
        rows.append(
            {
                "label": line.get("libelle"),
                "cells": [
                    {
                        "id": cell.get("id"),
                        "column_label": cell.get("display_column")
                        or cell.get("field"),
                        "text": None,
                        "amount": cell.get("amount"),
                        "bbox_norm": cell.get("bbox_norm"),
                        "support_type": "other",
                    }
                    for cell in line.get("cells", [])
                ],
            }
        )
    if rows:
        page.setdefault("tables", []).append(
            {
                "id": (
                    f"{page.get('period_role', 'N')}:p{page.get('page')}:{suffix}"
                ),
                "title": page.get("primary_title") or "Tableau reclassé",
                "bbox_norm": table_box,
                "rows": rows,
            }
        )
    page["page_role"] = role
    page["statement_kind"] = None
    page["fs_lines"] = []
    if scope is not None:
        page["scope"] = scope
    page.setdefault("quality_notes", []).append(note)


def _reclassify_secondary_statement_pages(
    canonical: dict[str, Any],
) -> dict[str, Any]:
    """Exclude management-detail and tax-form replicas from FS mapping."""

    tax_section = False
    pages = sorted(
        (page for page in canonical.get("pages", []) if isinstance(page, dict)),
        key=lambda page: int(page.get("page") or 0),
    )
    for page in pages:
        descriptor = _fold(
            " ".join(
                [
                    str(page.get("primary_title") or ""),
                    *[str(value) for value in page.get("headings", [])],
                    *[
                        str(block.get("text") or "")
                        for block in page.get("blocks", [])
                        if isinstance(block, dict)
                    ],
                ]
            )
        )
        if page.get("page_role") == "cover":
            if "etats fiscaux" in descriptor:
                tax_section = True
            elif any(
                marker in descriptor
                for marker in (
                    "annexe aux comptes",
                    "etats de gestion",
                    "details de comptes",
                    "comptes annuels",
                )
            ):
                tax_section = False
        if tax_section:
            page["scope"] = "tax"
        if page.get("page_role") != "primary_statement":
            continue
        if tax_section:
            _move_fs_lines_to_table(
                page,
                role="tax_form",
                scope="tax",
                suffix="tax_form_reclassified",
                note=(
                    "Primary-looking table reclassified as a tax form because it "
                    "belongs to the ETATS FISCAUX section."
                ),
            )
        elif "detaille" in descriptor or "soldes intermediaires de gestion" in descriptor:
            _move_fs_lines_to_table(
                page,
                role="annex_table",
                scope=None,
                suffix="management_detail_reclassified",
                note=(
                    "Detailed/management statement excluded from primary FS mapping "
                    "to avoid duplicating the statutory summary statements."
                ),
            )
    return canonical


def _reclassify_suspicious_primary_pages(
    canonical: dict[str, Any],
) -> dict[str, Any]:
    """Keep annex schedules from leaking into Bilan/CPC mapping.

    A page rendered without its surrounding pages can resemble a primary
    statement when it is actually a continuation of a numbered annex table.
    Reclassification is deliberately narrow: it requires an annex cover,
    no other confidently titled primary statement, no visible primary title
    on the candidate, and very sparse numeric FS cells.
    """

    pages = [page for page in canonical.get("pages", []) if isinstance(page, dict)]
    annex_cover = any(
        "annexe" in _fold(
            " ".join(
                [
                    str(page.get("primary_title") or ""),
                    *[str(value) for value in page.get("headings", [])],
                    *[
                        str(block.get("text") or "")
                        for block in page.get("blocks", [])
                        if isinstance(block, dict)
                    ],
                ]
            )
        )
        for page in pages[:3]
    )
    confident_primary = any(
        page.get("page_role") == "primary_statement"
        and any(
            marker in _fold(
                " ".join(
                    [
                        str(page.get("primary_title") or ""),
                        *[str(value) for value in page.get("headings", [])],
                    ]
                )
            )
            for marker in ("bilan actif", "bilan passif", "compte de resultat")
        )
        for page in pages
    )
    if not annex_cover or confident_primary:
        return canonical

    for page in pages:
        if page.get("page_role") != "primary_statement" or page.get("primary_title"):
            continue
        fs_lines = [line for line in page.get("fs_lines", []) if isinstance(line, dict)]
        numeric_lines = sum(
            any(cell.get("amount") is not None for cell in line.get("cells", []))
            for line in fs_lines
        )
        if not fs_lines or numeric_lines > max(3, len(fs_lines) // 5):
            continue

        _move_fs_lines_to_table(
            page,
            role="annex_table",
            scope=None,
            suffix="annex_reclassified",
            note=(
                "Page reclassified from primary statement to annex table because "
                "the document is annex-only and the untitled schedule has sparse "
                "numeric cells."
            ),
        )
    return canonical


def extract_canonical_documents(
    documents: dict[str, Path],
    *,
    output_paths: dict[str, Path],
    runs_dir: Path,
    settings: dict[str, Any],
    page_selection: dict[str, list[int]] | None = None,
) -> dict[str, Any]:
    all_jobs = [
        {"period_role": period_role, "pdf": pdf, "page": page}
        for period_role, pdf in documents.items()
        for page in (
            page_selection.get(period_role, [])
            if page_selection and page_selection.get(period_role)
            else range(1, _page_count(pdf) + 1)
        )
    ]
    jobs: list[dict[str, Any]] = []
    cached_pages: list[dict[str, Any]] = []
    for job in all_jobs:
        period_role = str(job["period_role"])
        page = int(job["page"])
        cache_path = (
            runs_dir
            / period_role
            / f"page_{page}"
            / f"canonical_{period_role}_page_{page}_parsed.json"
        )
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            cached = None
        if isinstance(cached, dict):
            cached_pages.append(
                _normalize_page(
                    cached,
                    page=page,
                    period_role=period_role,
                    source=Path(job["pdf"]),
                )
            )
        else:
            jobs.append(job)
    if cached_pages:
        print(
            f"[canonical] resuming with {len(cached_pages)} cached page(s); "
            f"{len(jobs)} page call(s) remain",
            flush=True,
        )
    prompt = prompt_path(settings, "canonical_page_prompt")

    def operation(
        job: dict[str, Any], account_settings: dict[str, Any], account_name: str
    ) -> dict[str, Any]:
        period_role = str(job["period_role"])
        pdf = Path(job["pdf"])
        page = int(job["page"])
        print(
            f"[canonical] {period_role} page {page} via Copilot account {account_name}"
        )
        with tempfile.TemporaryDirectory(prefix="stat_canonical_page_") as temp_dir:
            image = Path(temp_dir) / f"{period_role}_page_{page}_grid.png"
            render_page_grid(pdf, page, image)
            parsed = run_copilot_single_page_extraction(
                image,
                prompt,
                runs_dir / period_role / f"page_{page}",
                account_settings,
                page=page,
                tag=f"canonical_{period_role}",
                prompt_context=(
                    f"This is period role {period_role}. The physical PDF page is {page}. "
                    "Return one canonical page object, not a full-document array."
                ),
            )
        return _normalize_page(
            parsed, page=page, period_role=period_role, source=pdf
        )

    pool: PoolRun[dict[str, Any]] = run_account_pool(jobs, settings, operation)
    pages_by_period: dict[str, list[dict[str, Any]]] = {
        period_role: [] for period_role in documents
    }
    for page in [*cached_pages, *pool.results]:
        pages_by_period[str(page["period_role"])].append(page)

    for period_role, pages in pages_by_period.items():
        document = {
            "document_type": "canonical_financial_report",
            "schema_version": 1,
            "period_role": period_role,
            "source": str(documents[period_role]),
            "pages": sorted(pages, key=lambda item: int(item["page"])),
        }
        write_json(output_paths[period_role], document)

    return {
        "vision_calls": len(jobs),
        "total_vision_pages": len(all_jobs),
        "cached_vision_pages": len(cached_pages),
        "elapsed_seconds": round(pool.elapsed_seconds, 3),
        "accounts": pool.account_stats,
        "documents": {key: str(value) for key, value in output_paths.items()},
    }


def derive_document_index(canonical: dict[str, Any], *, source: str) -> dict[str, Any]:
    pages = []
    for page in canonical.get("pages", []):
        pages.append(
            {
                "page": page.get("page"),
                "scope": page.get("scope", "other"),
                "page_role": page.get("page_role", "other"),
                "primary_title": page.get("primary_title"),
                "headings": page.get("headings", []),
                "entity": page.get("entity"),
                "period_end": page.get("period_end"),
                "statement_kind": page.get("statement_kind"),
                "reviewable": bool(page.get("reviewable", True)),
                "notes": "Derived from canonical Copilot page extraction.",
            }
        )
    return {
        "document_type": "financial_report_index",
        "source": source,
        "entity": next((page.get("entity") for page in pages if page.get("entity")), None),
        "period_end": next(
            (page.get("period_end") for page in pages if page.get("period_end")), None
        ),
        "pages": pages,
        "quality_notes": [],
    }


def derive_fs_extraction(canonical: dict[str, Any], *, source: str) -> dict[str, Any]:
    lines: list[dict[str, Any]] = []
    quality_notes: list[str] = []
    for page in canonical.get("pages", []):
        quality_notes.extend(page.get("quality_notes", []))
        for line in page.get("fs_lines", []):
            values = {field: None for field in FS_FIELDS}
            for cell in line.get("cells", []):
                field = cell.get("field")
                if field in values:
                    values[field] = cell.get("amount")
            if not any(value is not None for value in values.values()):
                continue
            lines.append(
                {
                    "statement": line.get("statement"),
                    "scope": page.get("scope"),
                    "statement_set_id": page.get("statement_set_id"),
                    "entity": page.get("entity"),
                    "page": page.get("page"),
                    "libelle": line.get("libelle"),
                    "brut": values["brut"],
                    "amortissement": values["amortissement"],
                    "montant_n": values["montant_n"],
                    "montant_n_1": values["montant_n_1"],
                    "is_total": bool(line.get("is_total", False)),
                    "notes": "Derived from canonical Copilot page extraction.",
                    "canonical_id": line.get("id"),
                }
            )
    return {
        "source": source,
        "extraction_method": "Canonical Copilot vision over one gridded image per page",
        "lines": lines,
        "quality_notes": quality_notes,
    }


def derive_layout(
    canonical: dict[str, Any], *, pdf_path: Path, source: str
) -> dict[str, Any]:
    layouts = []
    for page in canonical.get("pages", []):
        entries = []
        for line in page.get("fs_lines", []):
            for cell in line.get("cells", []):
                entries.append(
                    {
                        "statement": line.get("statement"),
                        "scope": page.get("scope"),
                        "statement_set_id": page.get("statement_set_id"),
                        "page": page.get("page"),
                        "libelle": line.get("libelle"),
                        "field": cell.get("field"),
                        "display_column": cell.get("display_column"),
                        "amount": cell.get("amount"),
                        "amount_bbox_norm": cell.get("bbox_norm"),
                        "canonical_id": cell.get("id"),
                    }
                )
        layouts.append(
            normalize_layout(
                {"entries": entries, "quality_notes": page.get("quality_notes", [])},
                pdf_path=pdf_path,
                source=source,
                override_page=int(page["page"]),
            )
        )
    return merge_layouts(layouts, source=source)


def derive_pipeline_artifacts(
    canonical_path: Path,
    *,
    pdf_path: Path,
    fs_path: Path,
    layout_path: Path | None,
    index_path: Path,
) -> None:
    import json

    canonical = json.loads(canonical_path.read_text(encoding="utf-8-sig"))
    canonical = _reclassify_secondary_statement_pages(canonical)
    canonical = _reclassify_suspicious_primary_pages(canonical)
    canonical = refine_canonical_document(canonical, pdf_path=pdf_path)
    write_json(canonical_path, canonical)
    source = str(pdf_path)
    write_json(fs_path, derive_fs_extraction(canonical, source=source))
    write_json(index_path, derive_document_index(canonical, source=source))
    if layout_path is not None:
        write_json(
            layout_path,
            derive_layout(canonical, pdf_path=pdf_path, source=source),
        )
