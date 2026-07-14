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


def _fold(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = re.sub(r"\b(?:19|20)\d{2}\b", " year ", text)
    text = re.sub(r"\b\d{1,2}[./-]\d{1,2}[./-](?:\d{2}|\d{4})\b", " date ", text)
    text = re.sub(r"\b(?:suite|continued|continuation)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _ratio(left: Any, right: Any) -> float:
    a = _fold(left)
    b = _fold(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.88 + 0.12 * min(len(a), len(b)) / max(len(a), len(b))
    return SequenceMatcher(None, a, b).ratio()


def _heading_score(current: dict[str, Any], prior: dict[str, Any]) -> float:
    left = current.get("headings") or []
    right = prior.get("headings") or []
    if not left or not right:
        return 0.0
    scores = [max(_ratio(item, candidate) for candidate in right) for item in left]
    return sum(scores) / len(scores)


def _compatible_scope(current: dict[str, Any], prior: dict[str, Any]) -> bool:
    return str(current.get("scope")) == str(prior.get("scope"))


def _page_score(current: dict[str, Any], prior: dict[str, Any]) -> float:
    if not _compatible_scope(current, prior):
        return -1.0
    role_current = str(current.get("page_role") or "")
    role_prior = str(prior.get("page_role") or "")
    statement_current = str(current.get("statement_kind") or "")
    statement_prior = str(prior.get("statement_kind") or "")
    title = _ratio(current.get("primary_title"), prior.get("primary_title"))
    headings = _heading_score(current, prior)
    role = 1.0 if role_current == role_prior else 0.0
    statement = (
        1.0
        if statement_current and statement_current == statement_prior
        else 0.0
    )
    if role_current == "primary_statement" and statement_current != statement_prior:
        return -1.0
    return 0.55 * title + 0.25 * headings + 0.12 * role + 0.08 * statement


def align_pages(
    current_index: dict[str, Any], prior_index: dict[str, Any]
) -> dict[str, Any]:
    current_pages = current_index.get("pages", [])
    prior_pages = prior_index.get("pages", [])
    alignments: list[dict[str, Any]] = []
    last_prior_by_scope: dict[str, int] = {}

    for current in current_pages:
        scope = str(current.get("scope") or "other")
        ranked = sorted(
            (
                (_page_score(current, prior), prior)
                for prior in prior_pages
                if _compatible_scope(current, prior)
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        best_score, best = ranked[0] if ranked else (-1.0, None)
        threshold = 0.54
        if str(current.get("page_role")) == "accounting_policies":
            threshold = 0.43
        if best is not None and best_score >= threshold:
            prior_page = int(best["page"])
            previous = last_prior_by_scope.get(scope)
            if previous is not None and prior_page + 2 < previous and best_score < 0.8:
                best = None
            else:
                last_prior_by_scope[scope] = max(previous or 0, prior_page)
        if best is None or best_score < threshold:
            alignments.append(
                {
                    "current_page": int(current["page"]),
                    "prior_page": None,
                    "scope": scope,
                    "current_title": current.get("primary_title"),
                    "prior_title": None,
                    "score": round(max(best_score, 0.0), 3),
                    "match_type": "current_only",
                    "reason": "No sufficiently similar prior-year page in the same scope.",
                }
            )
            continue
        match_type = "same_page_kind"
        if int(best["page"]) in [
            item.get("prior_page") for item in alignments if item.get("prior_page")
        ]:
            match_type = "shared_prior_section"
        alignments.append(
            {
                "current_page": int(current["page"]),
                "prior_page": int(best["page"]),
                "scope": scope,
                "current_title": current.get("primary_title"),
                "prior_title": best.get("primary_title"),
                "score": round(best_score, 3),
                "match_type": match_type,
                "reason": "Matched by visual index scope, role, title, headings, and statement kind.",
            }
        )

    matched_prior = {item["prior_page"] for item in alignments if item["prior_page"]}
    unmatched_prior = [
        {
            "prior_page": int(page["page"]),
            "scope": page.get("scope"),
            "prior_title": page.get("primary_title"),
        }
        for page in prior_pages
        if int(page["page"]) not in matched_prior
    ]
    return {
        "current_source": current_index.get("source"),
        "prior_source": prior_index.get("source"),
        "alignments": alignments,
        "unmatched_prior_pages": unmatched_prior,
    }


def _read(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="Align current and N-1 visual page indexes.")
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--prior", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = align_pages(_read(args.current), _read(args.prior))
    write_json(args.output.resolve(), result)
    for item in result["alignments"]:
        print(
            f"N p{item['current_page']} -> "
            f"{('N-1 p' + str(item['prior_page'])) if item['prior_page'] else 'unmatched'} "
            f"({item['score']:.3f}) {item['current_title']}"
        )
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
