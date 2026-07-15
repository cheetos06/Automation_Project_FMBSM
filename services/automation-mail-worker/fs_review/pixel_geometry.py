from __future__ import annotations

import math
from collections import Counter
from pathlib import Path
from typing import Any

import fitz
import numpy as np


GEOMETRY_VERSION = 5


def _bbox(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 4:
        return None
    try:
        x1, y1, x2, y2 = (float(value[index]) for index in range(4))
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(item) and 0 <= item <= 1 for item in (x1, y1, x2, y2)):
        return None
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    return x1, y1, x2, y2


def _ink_mask(page: fitz.Page, *, zoom: float = 2.5) -> np.ndarray:
    pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    samples = np.frombuffer(pixmap.samples, dtype=np.uint8)
    image = samples.reshape(pixmap.height, pixmap.width, pixmap.n)
    rgb = image[:, :, :3]
    # Dark black text and saturated coloured headings both contain at least one
    # low channel. The clean source PDF has no coordinate grid at this stage.
    return np.min(rgb, axis=2) < 185


def _runs(columns: np.ndarray, *, max_gap: int) -> list[tuple[int, int]]:
    indexes = np.flatnonzero(columns)
    if indexes.size == 0:
        return []
    result: list[tuple[int, int]] = []
    start = int(indexes[0])
    previous = start
    for raw in indexes[1:]:
        current = int(raw)
        if current - previous > max_gap:
            result.append((start, previous))
            start = current
        previous = current
    result.append((start, previous))
    return result


def _line_candidates(mask: np.ndarray) -> list[dict[str, float]]:
    height, width = mask.shape
    clean = mask.copy()
    clean[clean.mean(axis=1) > 0.45, :] = False
    rows = np.flatnonzero(clean.sum(axis=1) > 2)
    if rows.size == 0:
        return []
    groups: list[tuple[int, int]] = []
    start = int(rows[0])
    previous = start
    max_gap = max(2, int(0.0015 * height))
    for raw in rows[1:]:
        row = int(raw)
        if row - previous > max_gap:
            groups.append((start, previous))
            start = row
        previous = row
    groups.append((start, previous))

    result = []
    for top, bottom in groups:
        columns = np.flatnonzero(clean[top : bottom + 1].any(axis=0))
        if columns.size == 0:
            continue
        result.append(
            {
                "left": int(columns[0]) / width,
                "right": int(columns[-1]) / width,
                "top": top / height,
                "bottom": bottom / height,
                "center": ((top + bottom) / 2) / height,
            }
        )
    return result


def _estimate_block_y_offset(
    mask: np.ndarray, blocks: list[dict[str, Any]]
) -> float:
    lines = _line_candidates(mask)
    offsets: list[float] = []
    for block in blocks:
        box = _bbox(block.get("bbox_norm"))
        if box is None:
            continue
        x1, y1, x2, y2 = box
        width = max(0.005, x2 - x1)
        center = (y1 + y2) / 2
        candidates = []
        for line in lines:
            line_width = max(0.003, line["right"] - line["left"])
            width_penalty = abs(math.log(line_width / width))
            y_distance = abs(line["center"] - center)
            left_distance = abs(line["left"] - x1)
            if width_penalty > 0.65 or y_distance > 0.09 or left_distance > 0.08:
                continue
            score = 2.5 * width_penalty + 4.0 * left_distance + 0.4 * y_distance
            candidates.append((score, line))
        if candidates:
            _, best = min(candidates, key=lambda item: item[0])
            offsets.append(center - best["center"])
    if len(offsets) < 3:
        return 0.0
    bins = Counter(round(offset / 0.005) for offset in offsets)
    winning_bin, count = bins.most_common(1)[0]
    if count < 2:
        return 0.0
    center = winning_bin * 0.005
    cluster = [offset for offset in offsets if abs(offset - center) <= 0.011]
    if len(cluster) < 2:
        return 0.0
    return float(np.median(np.asarray(cluster)))


def _assign_visible_rows(
    expected: list[float], candidates: list[float]
) -> list[float | None]:
    """Monotonically align extracted rows to visible rendered row centers."""

    if not expected or not candidates:
        return [None] * len(expected)
    row_candidates = sorted(
        value
        for value in candidates
        if any(abs(value - target) <= 0.10 for target in expected)
    )
    if len(row_candidates) < len(expected):
        return [None] * len(expected)

    count_expected = len(expected)
    count_candidates = len(row_candidates)
    infinity = float("inf")
    costs = [
        [infinity] * (count_candidates + 1) for _ in range(count_expected + 1)
    ]
    choices = [[False] * (count_candidates + 1) for _ in range(count_expected + 1)]
    for candidate_index in range(count_candidates + 1):
        costs[0][candidate_index] = 0.0
    for item_index in range(1, count_expected + 1):
        for candidate_index in range(1, count_candidates + 1):
            skip = costs[item_index][candidate_index - 1]
            match = costs[item_index - 1][candidate_index - 1] + abs(
                expected[item_index - 1] - row_candidates[candidate_index - 1]
            )
            if match <= skip:
                costs[item_index][candidate_index] = match
                choices[item_index][candidate_index] = True
            else:
                costs[item_index][candidate_index] = skip

    result: list[float | None] = [None] * count_expected
    item_index = count_expected
    candidate_index = count_candidates
    while item_index > 0 and candidate_index > 0:
        if choices[item_index][candidate_index]:
            result[item_index - 1] = row_candidates[candidate_index - 1]
            item_index -= 1
            candidate_index -= 1
        else:
            candidate_index -= 1
    return result


def refine_visible_bbox(
    mask: np.ndarray,
    value: Any,
    *,
    horizontal_gap_norm: float = 0.018,
    x_margin_norm: float = 0.018,
    y_margin_norm: float = 0.0025,
    max_right_extension_norm: float = 0.20,
    merge_all_runs: bool = False,
) -> list[float] | None:
    """Tighten an approximate visual box around its connected ink region."""

    box = _bbox(value)
    if box is None:
        return None
    height, width = mask.shape
    x1, y1, x2, y2 = box
    left = max(0, int((x1 - x_margin_norm) * width))
    right = min(
        width, int((max(x2, x1 + 0.04) + max_right_extension_norm) * width)
    )
    vertical_search = max(y_margin_norm, 0.04 if merge_all_runs else y_margin_norm)
    top = max(0, int((y1 - vertical_search) * height))
    bottom = min(height, int((y2 + vertical_search) * height) + 1)
    if right <= left or bottom <= top:
        return list(box)

    crop = mask[top:bottom, left:right].copy()
    if crop.size == 0:
        return list(box)

    # Ignore thin table rules or separators that span much of the crop.
    row_density = crop.mean(axis=1)
    crop[row_density > 0.45, :] = False
    if merge_all_runs:
        ink_rows = np.flatnonzero(crop.sum(axis=1) > 2)
        if ink_rows.size == 0:
            return list(box)
        row_groups: list[tuple[int, int]] = []
        group_start = int(ink_rows[0])
        previous = group_start
        max_row_gap = max(2, int(0.0015 * height))
        for raw_row in ink_rows[1:]:
            row = int(raw_row)
            if row - previous > max_row_gap:
                row_groups.append((group_start, previous))
                group_start = row
            previous = row
        row_groups.append((group_start, previous))

        candidates: list[dict[str, float | int]] = []
        original_width = max(0.005, x2 - x1)
        original_center = (y1 + y2) / 2
        for row_start, row_end in row_groups:
            line = crop[row_start : row_end + 1]
            columns = np.flatnonzero(line.any(axis=0))
            if columns.size == 0:
                continue
            line_left = (left + int(columns[0])) / width
            line_right = (left + int(columns[-1])) / width
            line_width = max(0.001, line_right - line_left)
            line_top = (top + row_start) / height
            line_bottom = (top + row_end) / height
            line_center = (line_top + line_bottom) / 2
            width_penalty = abs(math.log(line_width / original_width))
            score = (
                2.2 * width_penalty
                + 18.0 * abs(line_center - original_center)
                + 8.0 * abs(line_left - x1)
            )
            candidates.append(
                {
                    "row_start": row_start,
                    "row_end": row_end,
                    "left": int(columns[0]),
                    "right": int(columns[-1]),
                    "top_norm": line_top,
                    "bottom_norm": line_bottom,
                    "score": score,
                }
            )
        if not candidates:
            return list(box)
        best = min(candidates, key=lambda item: float(item["score"]))
        best_height = max(
            0.003, float(best["bottom_norm"]) - float(best["top_norm"])
        )
        expected_lines = max(
            1, min(8, round((y2 - y1) / (best_height + 0.004)))
        )
        selected_lines = [best]
        remaining = sorted(
            (item for item in candidates if item is not best),
            key=lambda item: (
                max(
                    0.0,
                    y1
                    - (float(item["top_norm"]) + float(item["bottom_norm"]))
                    / 2,
                    (float(item["top_norm"]) + float(item["bottom_norm"]))
                    / 2
                    - y2,
                ),
                abs(
                    (float(item["top_norm"]) + float(item["bottom_norm"])) / 2
                    - (float(best["top_norm"]) + float(best["bottom_norm"]))
                    / 2
                ),
            ),
        )
        while len(selected_lines) < expected_lines and remaining:
            current_top = min(float(item["top_norm"]) for item in selected_lines)
            current_bottom = max(float(item["bottom_norm"]) for item in selected_lines)
            current_left = min(int(item["left"]) for item in selected_lines)
            added = False
            for index, item in enumerate(remaining):
                item_top = float(item["top_norm"])
                item_bottom = float(item["bottom_norm"])
                vertical_gap = max(0.0, item_top - current_bottom, current_top - item_bottom)
                left_gap = abs(int(item["left"]) - current_left) / width
                if vertical_gap <= 0.018 and left_gap <= 0.04:
                    selected_lines.append(item)
                    remaining.pop(index)
                    added = True
                    break
            if not added:
                break
        run_left = min(int(item["left"]) for item in selected_lines)
        run_right = max(int(item["right"]) for item in selected_lines)
        selected_top = min(int(item["row_start"]) for item in selected_lines)
        selected_bottom = max(int(item["row_end"]) for item in selected_lines)
    else:
        columns = crop.any(axis=0)
        runs = _runs(columns, max_gap=max(3, int(horizontal_gap_norm * width)))
        if not runs:
            return list(box)
        seed_left = int(x1 * width) - left
        seed_right = int(max(x2, x1 + 0.04) * width) - left
        overlapping = [
            run for run in runs if run[1] >= seed_left and run[0] <= seed_right
        ]
        if not overlapping:
            center = (seed_left + seed_right) / 2
            selected = min(
                runs,
                key=lambda run: min(abs(run[0] - center), abs(run[1] - center)),
            )
        else:
            selected = (
                min(run[0] for run in overlapping),
                max(run[1] for run in overlapping),
            )
        run_left, run_right = selected
        selected_crop = crop[:, run_left : run_right + 1]
        rows = np.flatnonzero(selected_crop.any(axis=1))
        if rows.size == 0:
            return list(box)
        selected_top = int(rows[0])
        selected_bottom = int(rows[-1])

    refined = [
        max(0.0, (left + run_left - 2) / width),
        max(0.0, (top + selected_top - 2) / height),
        min(1.0, (left + run_right + 3) / width),
        min(1.0, (top + selected_bottom + 3) / height),
    ]
    if refined[2] - refined[0] < 0.003 or refined[3] - refined[1] < 0.002:
        return list(box)
    return [round(item, 5) for item in refined]


def refine_canonical_document(
    canonical: dict[str, Any], *, pdf_path: Path
) -> dict[str, Any]:
    """Refine canonical element geometry from visible pixels, without OCR."""

    if canonical.get("geometry_version") == GEOMETRY_VERSION:
        return canonical
    document = fitz.open(pdf_path)
    try:
        for page_data in canonical.get("pages", []):
            page_number = int(page_data["page"])
            mask = _ink_mask(document[page_number - 1])
            blocks = page_data.get("blocks", [])
            block_y_offset = _estimate_block_y_offset(mask, blocks)
            page_data["block_y_offset_norm"] = round(block_y_offset, 5)
            for block in blocks:
                raw_original = _bbox(block.get("bbox_norm"))
                original = (
                    (
                        raw_original[0],
                        max(0.0, raw_original[1] - block_y_offset),
                        raw_original[2],
                        min(1.0, raw_original[3] - block_y_offset),
                    )
                    if raw_original
                    else None
                )
                refined = refine_visible_bbox(
                    mask,
                    original,
                    y_margin_norm=0.005,
                    merge_all_runs=True,
                )
                refined_box = _bbox(refined)
                if original and refined_box and (
                    refined_box[2] - refined_box[0]
                    < 0.40 * (original[2] - original[0])
                    or refined_box[3] - refined_box[1]
                    < 0.40 * (original[3] - original[1])
                ):
                    refined = list(original)
                if refined:
                    block["bbox_norm"] = refined
            fs_lines = page_data.get("fs_lines", [])
            label_boxes = [
                {"bbox_norm": line.get("label_bbox_norm")}
                for line in fs_lines
                if _bbox(line.get("label_bbox_norm")) is not None
            ]
            fs_y_offset = 0.0
            if label_boxes:
                label_mask = mask.copy()
                label_right = max(
                    _bbox(item["bbox_norm"])[2]
                    for item in label_boxes
                    if _bbox(item["bbox_norm"]) is not None
                )
                cutoff = int(min(0.66, max(0.42, label_right + 0.06)) * mask.shape[1])
                label_mask[:, cutoff:] = False
                fs_y_offset = _estimate_block_y_offset(label_mask, label_boxes)
            page_data["fs_y_offset_norm"] = round(fs_y_offset, 5)

            value_lines = [line for line in fs_lines if line.get("cells")]
            expected_rows = []
            for line in value_lines:
                centers = [
                    (box[1] + box[3]) / 2
                    for cell in line.get("cells", [])
                    if (box := _bbox(cell.get("bbox_norm"))) is not None
                ]
                expected_rows.append(
                    (float(np.median(np.asarray(centers))) - fs_y_offset)
                    if centers
                    else 0.0
                )
            numeric_mask = mask.copy()
            cell_lefts = [
                box[0]
                for line in value_lines
                for cell in line.get("cells", [])
                if (box := _bbox(cell.get("bbox_norm"))) is not None
            ]
            if cell_lefts:
                numeric_left = int(max(0.30, min(cell_lefts) - 0.04) * mask.shape[1])
                numeric_mask[:, :numeric_left] = False
            visible_numeric_rows = [
                item["center"]
                for item in _line_candidates(numeric_mask)
                if item["bottom"] - item["top"] <= 0.03
            ]
            assignments = _assign_visible_rows(expected_rows, visible_numeric_rows)
            assigned_rows = {
                id(line): row for line, row in zip(value_lines, assignments)
            }

            for line in fs_lines:
                raw_label = _bbox(line.get("label_bbox_norm"))
                assigned_row = assigned_rows.get(id(line))
                shifted_label = (
                    (
                        raw_label[0],
                        max(
                            0.0,
                            (
                                assigned_row - (raw_label[3] - raw_label[1]) / 2
                                if assigned_row is not None
                                else raw_label[1] - fs_y_offset
                            ),
                        ),
                        raw_label[2],
                        min(
                            1.0,
                            (
                                assigned_row + (raw_label[3] - raw_label[1]) / 2
                                if assigned_row is not None
                                else raw_label[3] - fs_y_offset
                            ),
                        ),
                    )
                    if raw_label
                    else None
                )
                refined_label = refine_visible_bbox(
                    mask,
                    shifted_label,
                    horizontal_gap_norm=0.012,
                    y_margin_norm=0.025,
                    max_right_extension_norm=0.12,
                    merge_all_runs=True,
                )
                if refined_label:
                    line["label_bbox_norm"] = refined_label
                cells = line.get("cells", [])
                raw_cell_boxes = [_bbox(cell.get("bbox_norm")) for cell in cells]
                for cell_index, cell in enumerate(cells):
                    raw_cell = _bbox(cell.get("bbox_norm"))
                    shifted_cell = (
                        (
                            raw_cell[0],
                            max(
                                0.0,
                                (
                                    assigned_row - (raw_cell[3] - raw_cell[1]) / 2
                                    if assigned_row is not None
                                    else raw_cell[1] - fs_y_offset
                                ),
                            ),
                            raw_cell[2],
                            min(
                                1.0,
                                (
                                    assigned_row + (raw_cell[3] - raw_cell[1]) / 2
                                    if assigned_row is not None
                                    else raw_cell[3] - fs_y_offset
                                ),
                            ),
                        )
                        if raw_cell
                        else None
                    )
                    max_right_extension = 0.045
                    if raw_cell is not None and cell_index + 1 < len(raw_cell_boxes):
                        next_box = raw_cell_boxes[cell_index + 1]
                        if next_box is not None:
                            current_center = (raw_cell[0] + raw_cell[2]) / 2
                            next_center = (next_box[0] + next_box[2]) / 2
                            column_boundary = (current_center + next_center) / 2
                            max_right_extension = max(
                                0.002, min(0.045, column_boundary - raw_cell[2])
                            )
                    refined = refine_visible_bbox(
                        mask,
                        shifted_cell,
                        horizontal_gap_norm=0.009,
                        x_margin_norm=0.018,
                        y_margin_norm=0.008,
                        max_right_extension_norm=max_right_extension,
                        merge_all_runs=True,
                    )
                    if refined:
                        cell["bbox_norm"] = refined
            for table in page_data.get("tables", []):
                for row in table.get("rows", []):
                    for cell in row.get("cells", []):
                        refined = refine_visible_bbox(
                            mask,
                            cell.get("bbox_norm"),
                            horizontal_gap_norm=0.009,
                            x_margin_norm=0.012,
                            y_margin_norm=0.002,
                        )
                        if refined:
                            cell["bbox_norm"] = refined
    finally:
        document.close()
    canonical["geometry_method"] = "Copilot element identification plus rendered-pixel bounds"
    canonical["geometry_version"] = GEOMETRY_VERSION
    return canonical
