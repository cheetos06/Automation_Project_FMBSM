"""Add BG/RCA reconciliation tickmarks to a financial-statement PDF."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import fitz
import openpyxl


RED = (1.0, 0.0, 0.0)
GREEN = (0.0, 0.58, 0.25)
YELLOW = (1.0, 0.90, 0.25)
AUTHOR = "BG/FS Mapper"


def load_tick_rows(path: Path) -> list[dict[str, Any]]:
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheet = workbook.active
    values = list(sheet.iter_rows(values_only=True))
    workbook.close()
    if not values:
        return []
    headers = [str(value or "").strip() for value in values[0]]
    result = []
    for raw in values[1:]:
        item = {
            headers[index]: raw[index] if index < len(raw) else None
            for index in range(len(headers))
        }
        if item.get("page") and item.get("tick x") is not None and item.get("tick y") is not None:
            result.append(item)
    return result


def _set_info(annotation: Any, subject: str, comment: str) -> None:
    annotation.set_info(
        title=AUTHOR,
        subject=subject,
        content=comment or subject,
    )
    annotation.update()


def _ink(
    page: fitz.Page,
    strokes: list[list[tuple[float, float]]],
    color: tuple[float, float, float],
    subject: str,
    comment: str,
    width: float = 1.4,
) -> Any:
    annotation = page.add_ink_annot(strokes)
    annotation.set_colors(stroke=color)
    annotation.set_border(width=width)
    _set_info(annotation, subject, comment)
    return annotation


def add_check(
    page: fitz.Page,
    x: float,
    y: float,
    color: tuple[float, float, float],
    subject: str,
    comment: str,
    boxed: bool = False,
) -> None:
    if boxed:
        rectangle = page.add_rect_annot(fitz.Rect(x - 5, y - 5, x + 5, y + 5))
        rectangle.set_colors(stroke=color)
        rectangle.set_border(width=1.1)
        _set_info(rectangle, subject, comment)
        size = 3.3
    else:
        size = 5.0
    _ink(
        page,
        [[(x - size, y), (x - 1.2, y + size * 0.65), (x + size, y - size)]],
        color,
        subject,
        comment,
    )


def add_difference(
    page: fitz.Page, x: float, y: float, subject: str, comment: str
) -> None:
    rectangle = page.add_rect_annot(fitz.Rect(x - 5, y - 5, x + 5, y + 5))
    rectangle.set_colors(stroke=RED)
    rectangle.set_border(width=1.2)
    _set_info(rectangle, subject, comment)
    _ink(
        page,
        [
            [(x - 3, y - 3), (x + 3, y + 3)],
            [(x + 3, y - 3), (x - 3, y + 3)],
        ],
        RED,
        subject,
        comment,
    )


def add_calculation(
    page: fitz.Page, x: float, y: float, subject: str, comment: str
) -> None:
    _ink(
        page,
        [
            [(x, y - 4), (x, y + 4)],
            [(x - 4, y + 4), (x + 4, y + 4)],
        ],
        RED,
        subject,
        comment or "Total recalcule.",
    )


def add_suspense(
    page: fitz.Page, x: float, y: float, subject: str, comment: str
) -> None:
    rectangle = page.add_rect_annot(fitz.Rect(x - 7, y - 4, x + 7, y + 4))
    rectangle.set_colors(stroke=YELLOW, fill=YELLOW)
    rectangle.set_opacity(0.35)
    _set_info(rectangle, subject, comment or "A revoir.")


def _row_region(item: dict[str, Any]) -> fitz.Rect | None:
    values = [item.get(key) for key in ("region x1", "region y1", "region x2", "region y2")]
    if any(value is None or value == "" for value in values):
        return None
    try:
        x1, y1, x2, y2 = (float(value) for value in values)
    except (TypeError, ValueError):
        return None
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    return fitz.Rect(x1, y1, x2, y2)


def add_difference_region(
    page: fitz.Page,
    region: fitz.Rect,
    x: float,
    y: float,
    subject: str,
    comment: str,
) -> None:
    rectangle = page.add_rect_annot(region)
    rectangle.set_colors(stroke=RED)
    rectangle.set_border(width=1.1)
    _set_info(rectangle, subject, comment)
    add_difference(page, x, y, subject, comment)


def add_suspense_region(
    page: fitz.Page,
    region: fitz.Rect,
    x: float,
    y: float,
    subject: str,
    comment: str,
) -> None:
    rectangle = page.add_rect_annot(region)
    rectangle.set_colors(stroke=YELLOW, fill=YELLOW)
    rectangle.set_border(width=1.2)
    rectangle.set_opacity(0.12)
    _set_info(rectangle, subject, comment or "Piece justificative manquante.")
    add_suspense(page, x, y, subject, comment)


def add_tick(page: fitz.Page, item: dict[str, Any]) -> None:
    x = float(item["tick x"])
    y = float(item["tick y"])
    tickmark = str(item.get("tickmark") or "")
    status = str(item.get("status") or "")
    line = str(item.get("fs line") or item.get("label") or "")
    column = str(item.get("display column") or "")
    comment = str(item.get("comment") or "")
    subject = f"{status}: {line} ({column})"
    region = _row_region(item)
    if tickmark == "difference" and region is not None:
        add_difference_region(page, region, x, y, subject, comment)
        return
    if tickmark == "suspense" and region is not None:
        add_suspense_region(page, region, x, y, subject, comment)
        return
    if tickmark == "n_match":
        add_check(page, x, y, RED, subject, comment)
    elif tickmark == "n1_match":
        add_check(page, x, y, RED, subject, comment, boxed=True)
    elif tickmark == "calculation":
        add_calculation(page, x, y, subject, comment)
    elif tickmark == "n_coherent":
        add_check(page, x, y, GREEN, subject, comment)
    elif tickmark == "wording_same":
        add_check(page, x, y, GREEN, subject, comment)
    elif tickmark == "wording_new":
        add_check(page, x, y, GREEN, subject, comment)
    elif tickmark == "difference":
        add_difference(page, x, y, subject, comment)
    else:
        add_suspense(page, x, y, subject, comment)


def add_legend(page: fitz.Page) -> None:
    width = min(242.0, page.rect.width - 12.0)
    left = max(6.0, page.rect.width - width - 6.0)
    box = fitz.Rect(left, 6, left + width, 112)
    page.draw_rect(box, color=RED, width=0.8, overlay=True)
    columns = [
        [
            ("n_match", "OK BG N"),
            ("n1_match", "OK RCA N-1"),
            ("calculation", "OK calcul"),
            ("n_coherent", "OK coherent N"),
        ],
        [
            ("wording_same", "Wording idem N-1"),
            ("wording_new", "Wording nouveau N"),
            ("suspense", "Piece manquante"),
            ("difference", "Ecart a remonter"),
        ],
    ]
    column_width = width / 2
    for column_index, entries in enumerate(columns):
        tick_x = left + 13 + column_index * column_width
        text_x = tick_x + 14
        for row_index, (tickmark, label) in enumerate(entries):
            y = 23 + row_index * 25
            item = {
                "tick x": tick_x,
                "tick y": y,
                "tickmark": tickmark,
                "status": label,
                "fs line": "",
                "display column": "",
                "comment": label,
            }
            add_tick(page, item)
            page.insert_text(
                (text_x, y + 3),
                label,
                fontsize=6.8,
                fontname="cour",
                color=(0, 0, 0),
                overlay=True,
            )


def tick_pdf(
    source_pdf: Path,
    fs_mapping: Path | list[Path],
    output_pdf: Path,
    include_legend: bool = True,
    review_mapping: Path | None = None,
) -> int:
    document = fitz.open(source_pdf)
    mapping_paths = fs_mapping if isinstance(fs_mapping, list) else [fs_mapping]
    rows = []
    for mapping_path in mapping_paths:
        rows.extend(load_tick_rows(mapping_path))
    if review_mapping is not None and review_mapping.exists():
        rows.extend(load_tick_rows(review_mapping))
    for item in rows:
        page_number = int(item["page"])
        if page_number < 1 or page_number > document.page_count:
            raise ValueError(f"Invalid page {page_number} for {source_pdf}")
        add_tick(document[page_number - 1], item)
    if include_legend and document.page_count:
        add_legend(document[0])
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    document.save(output_pdf, garbage=4, deflate=True)
    document.close()
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Add FS reconciliation tickmarks from FS_Mapped.xlsx."
    )
    parser.add_argument("--pdf", type=Path, required=True, help="Unpointed FS PDF")
    parser.add_argument(
        "--fs-mapped", type=Path, required=True, help="FS_Mapped.xlsx path"
    )
    parser.add_argument("--output", type=Path, required=True, help="Output PDF")
    parser.add_argument(
        "--review-mapped",
        type=Path,
        help="Optional Document_Review.xlsx with wording/table review ticks",
    )
    parser.add_argument(
        "--no-legend", action="store_true", help="Do not add the first-page legend"
    )
    args = parser.parse_args()
    count = tick_pdf(
        args.pdf.resolve(),
        args.fs_mapped.resolve(),
        args.output.resolve(),
        not args.no_legend,
        args.review_mapped.resolve() if args.review_mapped else None,
    )
    print(f"Added {count} tickmarks to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
