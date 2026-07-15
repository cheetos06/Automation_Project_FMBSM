from __future__ import annotations

import argparse
import json
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from copilot_extract import write_json


PIPELINE_DIR = Path(__file__).resolve().parent
FIELDS = ("brut", "amortissement", "montant_n", "montant_n_1")


def _fold(value: Any) -> str:
    text = str(value or "")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _short_label(value: Any) -> str:
    text = _fold(value)
    text = re.sub(r"\b(i|ii|iii|iv|v|vi|vii|viii|ix|x|i bis)\b$", "", text)
    text = re.sub(r"\b(i a iv|i a vii)\b$", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _statement(value: Any) -> str:
    folded = _fold(value)
    if "actif" in folded:
        return "actif"
    if "passif" in folded:
        return "passif"
    if "resultat" in folded or "cpc" in folded:
        return "resultat"
    return folded


def _scope(value: Any) -> str:
    folded = _fold(value)
    if "consolid" in folded:
        return "consolidated"
    if any(word in folded for word in ("annual", "social", "individuel")):
        return "annual"
    return folded


def _is_reference_note_label(value: Any) -> bool:
    raw = str(value or "").strip()
    folded = _fold(raw)
    return (
        bool(re.match(r"^\(?\d+\)", raw))
        or raw.startswith("*")
        or raw.startswith("-")
        or folded.startswith("dont ")
        or folded.startswith("y compris")
        or "redevances de credit bail" in folded
        or "concernant les entites liees" in folded
    )


def _legacy_coordinate_note(value: Any) -> bool:
    text = str(value or "")
    return text.startswith("Snapped ") or text.startswith("Repaired ")


def _quality_notes(layout: Any) -> list[Any]:
    if not isinstance(layout, dict) or not isinstance(layout.get("quality_notes"), list):
        return []
    return [
        note
        for note in layout.get("quality_notes", [])
        if not _legacy_coordinate_note(note)
    ]


def _score(layout_label: str, fs_label: str) -> float:
    left = _fold(layout_label)
    right = _fold(fs_label)
    left_short = _short_label(layout_label)
    right_short = _short_label(fs_label)
    if left == right:
        return 100.0
    if left_short and left_short == right_short:
        return 95.0
    if left and right and (left.startswith(right) or right.startswith(left)):
        return 90.0 + min(len(left), len(right)) / max(len(left), len(right))
    if left_short and right_short and (
        left_short.startswith(right_short) or right_short.startswith(left_short)
    ):
        return 85.0 + min(len(left_short), len(right_short)) / max(
            len(left_short), len(right_short)
        )
    return SequenceMatcher(None, left, right).ratio() * 80.0


def _fs_lines(fs_json: Path) -> list[dict[str, Any]]:
    data = json.loads(fs_json.read_text(encoding="utf-8-sig"))
    lines = data.get("lines", []) if isinstance(data, dict) else data
    return [item for item in lines if isinstance(item, dict)]


def align_layout(layout_json: Path, fs_json: Path) -> dict[str, Any]:
    layout = json.loads(layout_json.read_text(encoding="utf-8-sig"))
    entries = layout.get("entries", []) if isinstance(layout, dict) else layout
    fs_lines = _fs_lines(fs_json)
    fs_pages = {
        int(line["page"])
        for line in fs_lines
        if isinstance(line.get("page"), int)
        or (isinstance(line.get("page"), str) and str(line.get("page")).isdigit())
    }
    changed = 0
    filtered_entries = []
    removed = 0
    removed_non_fs_page = 0

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            entry_page = int(entry.get("page"))
        except (TypeError, ValueError):
            entry_page = None
        if fs_pages and entry_page not in fs_pages:
            removed_non_fs_page += 1
            continue
        if _is_reference_note_label(entry.get("libelle")):
            removed += 1
            continue
        field = str(entry.get("field") or "montant_n")
        statement = _statement(entry.get("statement"))
        scope = _scope(entry.get("scope"))
        candidates = []
        for line in fs_lines:
            if field not in FIELDS or line.get(field) is None:
                continue
            fs_statement = _statement(line.get("statement"))
            if statement and fs_statement and statement != fs_statement:
                continue
            fs_scope = _scope(line.get("scope"))
            if scope and fs_scope and scope != fs_scope:
                continue
            candidates.append(line)
        ranked = sorted(
            (
                (
                    _score(
                        str(entry.get("libelle") or ""),
                        str(line.get("libelle") or ""),
                    ),
                    line,
                )
                for line in candidates
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        if not ranked or ranked[0][0] < 84.0:
            continue
        if len(ranked) > 1 and abs(ranked[0][0] - ranked[1][0]) < 0.01:
            continue
        canonical = str(ranked[0][1].get("libelle") or "").strip()
        if canonical and canonical != entry.get("libelle"):
            entry["libelle"] = canonical
            changed += 1
        filtered_entries.append(entry)

    return {
        "coordinate_system": layout.get(
            "coordinate_system", "PDF points, origin at top-left"
        ),
        "source": layout.get("source"),
        "entries": filtered_entries,
        "quality_notes": _quality_notes(layout)
        + [
            f"Aligned {changed} layout labels to canonical FS extraction labels.",
            f"Removed {removed} explanatory note/reference layout entries.",
            f"Removed {removed_non_fs_page} layout entries from pages not identified as Bilan/CPC pages.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Align Copilot layout labels to Copilot FS extraction labels."
    )
    parser.add_argument(
        "--layout",
        type=Path,
        default=PIPELINE_DIR.parent
        / "Examples"
        / "17779"
        / "Output"
        / "extraction"
        / "fs_tick_layout.json",
    )
    parser.add_argument(
        "--fs-json",
        type=Path,
        default=PIPELINE_DIR.parent
        / "Examples"
        / "17779"
        / "Output"
        / "extraction"
        / "fs_extract_N.json",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or args.layout
    aligned = align_layout(args.layout.resolve(), args.fs_json.resolve())
    write_json(output.resolve(), aligned)
    print(
        f"[layout-align] wrote {len(aligned.get('entries', []))} entries to {output.resolve()}"
    )
    print("[layout-align] " + aligned["quality_notes"][-1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
