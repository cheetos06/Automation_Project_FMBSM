from __future__ import annotations

import argparse
import json
import math
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from copilot_extract import write_json


PIPELINE_DIR = Path(__file__).resolve().parent


def _fold(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _statement(value: Any) -> str:
    text = _fold(value)
    if "actif" in text:
        return "actif"
    if "passif" in text:
        return "passif"
    if "resultat" in text or "cpc" in text:
        return "resultat"
    return text


def _scope(value: Any) -> str:
    text = _fold(value)
    if "consolid" in text:
        return "consolidated"
    if any(word in text for word in ("annual", "social", "individuel")):
        return "annual"
    return text


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _amount_match(left: Any, right: Any) -> bool:
    a = _number(left)
    b = _number(right)
    return a is not None and b is not None and abs(a - b) <= 1.0


def _label_score(left: Any, right: Any) -> float:
    l = _fold(left)
    r = _fold(right)
    left_roman = _roman_marker(left)
    right_roman = _roman_marker(right)
    if l == r:
        score = 100.0
    elif l and r and (l in r or r in l):
        score = 90.0 + min(len(l), len(r)) / max(len(l), len(r))
    else:
        score = SequenceMatcher(None, l, r).ratio() * 80.0
    if left_roman and right_roman and left_roman == right_roman:
        score += 12.0
    if l.startswith("total") and r.startswith("total"):
        score += 5.0
    if "resultat" in l and "resultat" in r:
        score += 5.0
    return score


def _roman_marker(value: Any) -> str:
    text = _fold(value)
    tokens = text.split()
    romans = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"}
    found = [token for token in tokens if token in romans]
    return found[-1] if found else ""


def _lines(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    lines = data.get("lines", []) if isinstance(data, dict) else data
    return [item for item in lines if isinstance(item, dict)]


def align_prior(prior_json: Path, current_fs_json: Path) -> dict[str, Any]:
    prior_data = json.loads(prior_json.read_text(encoding="utf-8-sig"))
    prior_lines = prior_data.get("lines", []) if isinstance(prior_data, dict) else prior_data
    current_lines = _lines(current_fs_json)
    changed = 0

    current_candidates = [
        item
        for item in current_lines
        if item.get("montant_n_1") is not None
    ]
    for prior in prior_lines:
        if not isinstance(prior, dict) or prior.get("montant_n") is None:
            continue
        statement = _statement(prior.get("statement"))
        scope = _scope(prior.get("scope"))
        candidates = [
            current
            for current in current_candidates
            if _statement(current.get("statement")) == statement
            and (
                not scope
                or not _scope(current.get("scope"))
                or _scope(current.get("scope")) == scope
            )
            and _amount_match(prior.get("montant_n"), current.get("montant_n_1"))
        ]
        if not candidates:
            continue
        ranked = sorted(
            (
                (_label_score(prior.get("libelle"), current.get("libelle")), current)
                for current in candidates
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        if len(ranked) > 1 and abs(ranked[0][0] - ranked[1][0]) < 0.01:
            continue
        canonical = str(ranked[0][1].get("libelle") or "").strip()
        if canonical and canonical != prior.get("libelle"):
            prior["libelle"] = canonical
            changed += 1

    return {
        "source": prior_data.get("source") if isinstance(prior_data, dict) else None,
        "extraction_method": prior_data.get("extraction_method")
        if isinstance(prior_data, dict)
        else "Copilot vision over rendered PDF page images",
        "lines": prior_lines,
        "quality_notes": list(prior_data.get("quality_notes", []))
        + [f"Aligned {changed} RCA labels to current FS comparative labels."],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Align prior RCA labels to current FS N-1 labels."
    )
    parser.add_argument(
        "--prior-json",
        type=Path,
        default=PIPELINE_DIR.parent / "Examples" / "17779" / "Output" / "extraction" / "fs_extract_N_1.json",
    )
    parser.add_argument(
        "--current-fs-json",
        type=Path,
        default=PIPELINE_DIR.parent / "Examples" / "17779" / "Output" / "extraction" / "fs_extract_N.json",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or args.prior_json
    aligned = align_prior(args.prior_json.resolve(), args.current_fs_json.resolve())
    write_json(output.resolve(), aligned)
    print(
        f"[prior-align] wrote {len(aligned.get('lines', []))} lines to {output.resolve()}"
    )
    print("[prior-align] " + aligned["quality_notes"][-1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
