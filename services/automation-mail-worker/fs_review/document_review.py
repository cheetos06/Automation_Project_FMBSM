from __future__ import annotations

import json
import math
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

import fitz
import openpyxl

from annex_bg_evidence import (
    bg_supports_schedule_absence,
    load_bg,
    match_requirement_to_bg,
)
from copilot_extract import _repair_mojibake, write_json


REVIEW_HEADERS = [
    "page",
    "scope",
    "review kind",
    "label",
    "prior page",
    "status",
    "tickmark",
    "comment",
    "evidence",
    "required source",
    "tick x",
    "tick y",
    "region x1",
    "region y1",
    "region x2",
    "region y2",
]

KIND_TO_TICK = {
    "wording_same": "wording_same",
    "expected_rollforward": "wording_same",
    "wording_new": "wording_new",
    "difference": "difference",
    "prior_amount_match": "n1_match",
    "prior_amount_difference": "difference",
    "calculation_match": "calculation",
    "calculation_difference": "difference",
    "coherent_current": "n_coherent",
    "suspense": "suspense",
}


def _fold(value: Any) -> str:
    text = unicodedata.normalize("NFKD", _repair_mojibake(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


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


def _points(
    bbox: tuple[float, float, float, float], width: float, height: float
) -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = bbox
    return x1 * width, y1 * height, x2 * width, y2 * height


def _tick_anchor(
    region: tuple[float, float, float, float], width: float, height: float
) -> tuple[float, float]:
    _, y1, x2, y2 = region
    x = min(width - 9.0, x2 + 6.0)
    y = min(height - 7.0, max(7.0, (y1 + y2) / 2.0))
    return round(x, 2), round(y, 2)


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


def _item_anchor(
    item: dict[str, Any],
    region: tuple[float, float, float, float],
    width: float,
    height: float,
) -> tuple[float, float]:
    normalized = _point(item.get("tick_anchor_norm"))
    if normalized is None:
        return _tick_anchor(region, width, height)
    x = min(width - 9.0, max(9.0, normalized[0] * width))
    y = min(height - 7.0, max(7.0, normalized[1] * height))
    return round(x, 2), round(y, 2)


def _index_text(index: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for page in index.get("pages", []):
        result.append(str(page.get("primary_title") or ""))
        result.extend(str(item) for item in page.get("headings", []))
    return [item for item in result if _fold(item)]


def _exists_in_current(label: Any, current_text: list[str]) -> bool:
    target = _fold(label)
    if not target:
        return False
    if re.fullmatch(r"cadre [a-z0-9]+", target):
        return True
    for candidate in current_text:
        folded = _fold(candidate)
        if target == folded or target in folded or folded in target:
            return True
        if SequenceMatcher(None, target, folded).ratio() >= 0.86:
            return True
    return False


def _inside(
    inner: tuple[float, float, float, float],
    outer: tuple[float, float, float, float],
) -> bool:
    ix1, iy1, ix2, iy2 = inner
    ox1, oy1, ox2, oy2 = outer
    center_x = (ix1 + ix2) / 2
    center_y = (iy1 + iy2) / 2
    return ox1 <= center_x <= ox2 and oy1 <= center_y <= oy2


def _maturity_source(table_title: Any) -> str | None:
    folded = _fold(table_title)
    if "etat des creances" in folded:
        return "aged_receivables"
    if "etat des dettes" in folded:
        return "aged_payables"
    return None


def _specialized_source(table_title: Any, source: str, bg_support: str) -> tuple[str, str | None]:
    folded = _fold(table_title)
    if "variation des capitaux propres" in folded and bg_support == "partial":
        return (
            "legal_support",
            "La BG peut rapprocher certains soldes, mais les affectations, apports "
            "et autres mouvements de capitaux propres exigent les decisions "
            "juridiques et le tableau de variation detaille.",
        )
    if "liste des filiales" in folded:
        return (
            "subsidiary_fs",
            "Les pourcentages de detention exigent les justificatifs juridiques; "
            "les capitaux propres, chiffre d'affaires et resultat exigent les "
            "comptes des filiales.",
        )
    return source, None


def build_review_rows(
    comparisons: Iterable[dict[str, Any]],
    *,
    current_pdf: Path,
    current_index: dict[str, Any],
    available_sources: set[str] | None = None,
    bg_path: Path | None = None,
    bg_paths: dict[str, Path] | None = None,
) -> list[dict[str, Any]]:
    available = set(available_sources or {"bg", "prior_fs"})
    document = fitz.open(current_pdf)
    page_sizes = {
        index + 1: (float(page.rect.width), float(page.rect.height))
        for index, page in enumerate(document)
    }
    document.close()
    current_text = _index_text(current_index)
    fallback_bg_rows = load_bg(bg_path) if bg_path and bg_path.exists() else []
    scoped_bg_rows = {
        str(scope): load_bg(path)
        for scope, path in (bg_paths or {}).items()
        if path.exists()
    }
    index_by_page = {
        int(page["page"]): page for page in current_index.get("pages", [])
    }
    rows: list[dict[str, Any]] = []

    for comparison in comparisons:
        scope = str(comparison.get("scope") or "other")
        bg_rows = scoped_bg_rows.get(scope, fallback_bg_rows)
        prior_pages = comparison.get("prior_pages") or []
        prior_page_text = ", ".join(str(item) for item in prior_pages)
        requirements: list[tuple[int, tuple[float, float, float, float], str]] = []
        for requirement in comparison.get("evidence_requirements", []):
            if not isinstance(requirement, dict):
                continue
            try:
                page_number = int(requirement.get("page"))
            except (TypeError, ValueError):
                continue
            normalized = _bbox(requirement.get("bbox_norm"))
            if normalized is None or page_number not in page_sizes:
                continue
            if index_by_page.get(page_number, {}).get("statement_kind") in {
                "bilan_actif",
                "bilan_passif",
                "resultat",
            }:
                continue
            original_source = str(requirement.get("required_source") or "other_detail")
            bg_support = str(requirement.get("bg_support"))
            source, specialized_reason = _specialized_source(
                requirement.get("table_title"), original_source, bg_support
            )
            if specialized_reason:
                requirement = dict(requirement)
                requirement["reason"] = specialized_reason
            maturity_source = _maturity_source(requirement.get("table_title"))
            requirements.append((page_number, normalized, source))
            width, height = page_sizes[page_number]
            region = _points(normalized, width, height)
            tick_x, tick_y = _item_anchor(requirement, region, width, height)
            if original_source == "bg" and bg_support in {"full", "partial"}:
                matches, complete = match_requirement_to_bg(requirement, bg_rows)
                for match in matches:
                    item_bbox = _bbox(match["item"].get("bbox_norm"))
                    if item_bbox is None:
                        continue
                    item_region = _points(item_bbox, width, height)
                    item_x, item_y = _item_anchor(
                        match["item"], item_region, width, height
                    )
                    rows.append(
                        {
                            "page": page_number,
                            "scope": scope,
                            "review kind": "bg_annex_match",
                            "label": _repair_mojibake(match["item"].get("row_label")),
                            "prior page": prior_page_text,
                            "status": "matched",
                            "tickmark": "n_match",
                            "comment": "Montant justifie par les comptes BG "
                            + ", ".join(match["accounts"])
                            + ".",
                            "evidence": " | ".join(match["labels"]),
                            "required source": "bg",
                            "tick x": item_x,
                            "tick y": item_y,
                            "region x1": None,
                            "region y1": None,
                            "region x2": None,
                            "region y2": None,
                        }
                    )
                if complete and bg_support == "full" and maturity_source is None:
                    continue
                if maturity_source is not None:
                    source = maturity_source
                    requirement = dict(requirement)
                    requirement["reason"] = (
                        "Les soldes bruts peuvent etre rapproches a la BG, mais la "
                        "ventilation par echeance exige une balance agee ou un "
                        "echeancier detaille."
                    )
            rows.append(
                {
                    "page": page_number,
                    "scope": scope,
                    "review kind": "missing_evidence",
                    "label": _repair_mojibake(requirement.get("table_title")),
                    "prior page": prior_page_text,
                    "status": "review",
                    "tickmark": "suspense",
                    "comment": _repair_mojibake(requirement.get("reason")),
                    "evidence": "",
                    "required source": source,
                    "tick x": tick_x,
                    "tick y": tick_y,
                    "region x1": round(region[0], 2),
                    "region y1": round(region[1], 2),
                    "region x2": round(region[2], 2),
                    "region y2": round(region[3], 2),
                }
            )

        for annotation in comparison.get("annotations", []):
            if not isinstance(annotation, dict):
                continue
            kind = str(annotation.get("kind") or "suspense")
            try:
                page_number = int(annotation.get("page"))
            except (TypeError, ValueError):
                continue
            normalized = _bbox(annotation.get("bbox_norm"))
            if normalized is None or page_number not in page_sizes:
                continue
            page_descriptor = index_by_page.get(page_number, {})
            if not comparison.get("canonical_targets") and page_descriptor.get(
                "statement_kind"
            ) in {
                "bilan_actif",
                "bilan_passif",
                "resultat",
            }:
                label_folded = _fold(annotation.get("label"))
                title_folded = _fold(page_descriptor.get("primary_title"))
                if not (
                    label_folded in title_folded
                    or title_folded in label_folded
                    or SequenceMatcher(None, label_folded, title_folded).ratio()
                    >= 0.84
                ):
                    continue
            if kind == "coherent_current" and any(
                page_number == req_page
                and source not in available
                and _inside(normalized, req_bbox)
                for req_page, req_bbox, source in requirements
            ):
                continue
            width, height = page_sizes[page_number]
            region = _points(normalized, width, height)
            tick_x, tick_y = _item_anchor(annotation, region, width, height)
            is_region = kind in {"difference", "prior_amount_difference", "calculation_difference"}
            rows.append(
                {
                    "page": page_number,
                    "scope": scope,
                    "review kind": kind,
                    "label": _repair_mojibake(annotation.get("label")),
                    "prior page": prior_page_text,
                    "status": "difference" if "difference" in kind else "reviewed",
                    "tickmark": KIND_TO_TICK.get(kind, "suspense"),
                    "comment": _repair_mojibake(annotation.get("comment")),
                    "evidence": _repair_mojibake(annotation.get("evidence")),
                    "required source": "",
                    "tick x": tick_x,
                    "tick y": tick_y,
                    "region x1": round(region[0], 2) if is_region else None,
                    "region y1": round(region[1], 2) if is_region else None,
                    "region x2": round(region[2], 2) if is_region else None,
                    "region y2": round(region[3], 2) if is_region else None,
                }
            )

        for removed in comparison.get("removed_prior_blocks", []):
            if not isinstance(removed, dict) or _exists_in_current(
                removed.get("label"), current_text
            ):
                continue
            if bg_supports_schedule_absence(removed.get("label"), bg_rows):
                continue
            try:
                page_number = int(removed.get("page"))
            except (TypeError, ValueError):
                continue
            if index_by_page.get(page_number, {}).get("statement_kind") in {
                "bilan_actif",
                "bilan_passif",
                "resultat",
            }:
                continue
            normalized = _bbox(removed.get("anchor_bbox_norm"))
            if normalized is None or page_number not in page_sizes:
                continue
            width, height = page_sizes[page_number]
            region = _points(normalized, width, height)
            tick_x, tick_y = _item_anchor(removed, region, width, height)
            rows.append(
                {
                    "page": page_number,
                    "scope": scope,
                    "review kind": "removed_prior_block",
                    "label": _repair_mojibake(removed.get("label")),
                    "prior page": prior_page_text,
                    "status": "difference",
                    "tickmark": "difference",
                    "comment": _repair_mojibake(removed.get("comment")),
                    "evidence": "RCA N-1",
                    "required source": "",
                    "tick x": tick_x,
                    "tick y": tick_y,
                    "region x1": None,
                    "region y1": None,
                    "region x2": None,
                    "region y2": None,
                }
            )

    return _deduplicate(rows)


def _deduplicate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    priority = {
        "difference": 6,
        "suspense": 5,
        "calculation": 4,
        "n_match": 4,
        "n1_match": 3,
        "wording_new": 2,
        "wording_same": 1,
        "n_coherent": 1,
    }
    selected: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        key = (
            row.get("page"),
            _fold(row.get("label")),
            round(float(row.get("tick x") or 0) / 12),
            round(float(row.get("tick y") or 0) / 12),
            row.get("tickmark"),
        )
        previous = selected.get(key)
        if previous is None or priority.get(str(row.get("tickmark")), 0) > priority.get(
            str(previous.get("tickmark")), 0
        ):
            selected[key] = row
    return sorted(
        selected.values(),
        key=lambda item: (item["page"], item["tick y"], item["tick x"]),
    )


def write_review_workbook(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Document_Review"
    sheet.append(REVIEW_HEADERS)
    for item in rows:
        sheet.append([item.get(header) for header in REVIEW_HEADERS])
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for cell in sheet[1]:
        cell.font = openpyxl.styles.Font(bold=True)
    widths = [8, 16, 24, 48, 12, 14, 18, 72, 72, 28, 10, 10, 10, 10, 10, 10]
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[openpyxl.utils.get_column_letter(index)].width = width
    workbook.save(path)


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return data


def write_review_json(rows: list[dict[str, Any]], path: Path) -> None:
    write_json(path, {"rows": rows})
