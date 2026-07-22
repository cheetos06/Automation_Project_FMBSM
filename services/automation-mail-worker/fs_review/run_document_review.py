from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from align_document_pages import align_pages
from canonical_review import (
    build_review_jobs,
    resolve_comparison_targets,
    run_structured_review,
)
from copilot_extract import (
    load_settings,
    prompt_path,
    read_json,
    run_copilot_page_group_extraction,
    run_copilot_single_page_extraction,
    write_json,
)
from document_review import (
    build_review_rows,
    write_review_json,
    write_review_workbook,
)
from extract_document_index import extract_document_index
from refine_document_positions import refine_document_positions


PIPELINE_DIR = Path(__file__).resolve().parent


def _tag(current_pages: list[int], prior_pages: list[int]) -> str:
    current = "_".join(map(str, current_pages))
    if prior_pages:
        return f"compare_N_{current}_to_N1_" + "_".join(map(str, prior_pages))
    return f"review_current_N_{current}"


def _set_page_context(
    parsed: dict[str, Any],
    *,
    current_pages: list[int],
    prior_pages: list[int],
    scope: str,
) -> dict[str, Any]:
    parsed["current_pages"] = current_pages
    parsed["prior_pages"] = prior_pages
    parsed["scope"] = parsed.get("scope") or scope
    default_page = current_pages[0]
    for key in ("annotations", "evidence_requirements", "removed_prior_blocks"):
        for item in parsed.get(key, []):
            if isinstance(item, dict) and not item.get("page"):
                item["page"] = default_page
    return parsed


def _groups(
    alignment: dict[str, Any],
    current_index: dict[str, Any],
    prior_index: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    current_by_page = {
        int(page["page"]): page for page in current_index.get("pages", [])
    }
    prior_pages = (prior_index or {}).get("pages", [])
    primary_groups: dict[tuple[str, str], dict[str, Any]] = {}
    for page in current_by_page.values():
        if page.get("page_role") != "primary_statement" or not page.get(
            "statement_kind"
        ):
            continue
        key = (str(page.get("scope") or "other"), str(page["statement_kind"]))
        primary_groups.setdefault(
            key,
            {"current_pages": [], "prior_pages": [], "scope": key[0]},
        )["current_pages"].append(int(page["page"]))
    for page in prior_pages:
        key = (str(page.get("scope") or "other"), str(page.get("statement_kind")))
        if key in primary_groups and page.get("page_role") == "primary_statement":
            primary_groups[key]["prior_pages"].append(int(page["page"]))

    expanded_primary_groups: list[dict[str, Any]] = []
    for group in primary_groups.values():
        if group["prior_pages"] or len(group["current_pages"]) == 1:
            expanded_primary_groups.append(group)
        else:
            expanded_primary_groups.extend(
                {
                    "current_pages": [page],
                    "prior_pages": [],
                    "scope": group["scope"],
                }
                for page in group["current_pages"]
            )

    processed_current = {
        page
        for group in primary_groups.values()
        for page in group["current_pages"]
    }
    matched: dict[tuple[str, int], list[int]] = defaultdict(list)
    unmatched: list[dict[str, Any]] = []
    for item in alignment.get("alignments", []):
        current_page = int(item["current_page"])
        if current_page in processed_current:
            continue
        prior_page = item.get("prior_page")
        scope = str(item.get("scope") or "other")
        if prior_page is None:
            unmatched.append(
                {"current_pages": [current_page], "prior_pages": [], "scope": scope}
            )
        else:
            matched[(scope, int(prior_page))].append(current_page)
    groups = expanded_primary_groups + [
        {
            "current_pages": sorted(current_pages),
            "prior_pages": [prior_page],
            "scope": scope,
        }
        for (scope, prior_page), current_pages in matched.items()
    ]
    groups.extend(unmatched)
    return sorted(groups, key=lambda item: item["current_pages"][0])


def run_document_review(args: argparse.Namespace) -> dict[str, Any]:
    settings = load_settings(args.settings)
    current_pdf = args.current_pdf.resolve()
    prior_pdf = args.prior_pdf.resolve() if args.prior_pdf else None
    output_dir = args.output_dir.resolve()
    extraction_dir = output_dir / "extraction"
    runs_dir = output_dir / "copilot_runs" / "document_review"
    comparisons_dir = extraction_dir / "document_comparisons"
    extraction_dir.mkdir(parents=True, exist_ok=True)
    comparisons_dir.mkdir(parents=True, exist_ok=True)

    current_index_path = extraction_dir / "document_index_N.json"
    prior_index_path = extraction_dir / "document_index_N_1.json"
    alignment_path = extraction_dir / "document_page_alignment.json"
    current_canonical_path = (
        Path(args.current_canonical).resolve()
        if getattr(args, "current_canonical", None)
        else extraction_dir / "canonical_N.json"
    )
    prior_canonical_path = (
        Path(args.prior_canonical).resolve()
        if getattr(args, "prior_canonical", None)
        else extraction_dir / "canonical_N_1.json"
    )
    use_canonical = current_canonical_path.exists() and (
        prior_pdf is None or prior_canonical_path.exists()
    )

    if not current_index_path.exists() and not args.skip_copilot:
        extract_document_index(
            current_pdf,
            current_index_path,
            runs_dir / "document_index_N",
            settings,
            tag="document_index_N",
        )
    if prior_pdf and not prior_index_path.exists() and not args.skip_copilot:
        extract_document_index(
            prior_pdf,
            prior_index_path,
            runs_dir / "document_index_N_1",
            settings,
            tag="document_index_N_1",
        )
    if not current_index_path.exists():
        raise FileNotFoundError(f"Missing current visual index: {current_index_path}")

    current_index = read_json(current_index_path)
    if prior_pdf:
        if not prior_index_path.exists():
            raise FileNotFoundError(f"Missing prior visual index: {prior_index_path}")
        prior_index = read_json(prior_index_path)
        alignment = align_pages(current_index, prior_index)
    else:
        alignment = {
            "current_source": str(current_pdf),
            "prior_source": None,
            "alignments": [
                {
                    "current_page": page["page"],
                    "prior_page": None,
                    "scope": page.get("scope", "other"),
                }
                for page in current_index.get("pages", [])
            ],
            "unmatched_prior_pages": [],
        }
    write_json(alignment_path, alignment)

    comparisons: list[dict[str, Any]] = []
    current_canonical: dict[str, Any] | None = None
    prior_index = read_json(prior_index_path) if prior_pdf else None
    groups = _groups(alignment, current_index, prior_index)
    copilot_stats: dict[str, Any] = {}
    if use_canonical:
        current_canonical = read_json(current_canonical_path)
        prior_canonical = read_json(prior_canonical_path) if prior_pdf else None
        jobs = build_review_jobs(groups, current_canonical, prior_canonical)
        if args.skip_copilot:
            for job in jobs:
                comparison_path = comparisons_dir / f"{job['job_id']}.json"
                if not comparison_path.exists():
                    raise FileNotFoundError(
                        f"Missing structured comparison: {comparison_path}"
                    )
                comparisons.append(
                    resolve_comparison_targets(
                        read_json(comparison_path),
                        job=job,
                        current=current_canonical,
                    )
                )
        else:
            comparisons, copilot_stats = run_structured_review(
                jobs,
                current=current_canonical,
                comparisons_dir=comparisons_dir,
                runs_dir=runs_dir / "structured",
                settings=settings,
            )
        position_stats = {
            "position_pages": 0,
            "position_candidates": 0,
            "position_calls": 0,
        }
    else:
        for group in groups:
            current_pages = group["current_pages"]
            prior_pages = group["prior_pages"]
            scope = group["scope"]
            tag = _tag(current_pages, prior_pages)
            comparison_path = comparisons_dir / f"{tag}.json"
            if comparison_path.exists():
                parsed = read_json(comparison_path)
            elif args.skip_copilot:
                raise FileNotFoundError(f"Missing cached comparison: {comparison_path}")
            elif prior_pages and prior_pdf:
                print(
                    f"[document-review] Copilot section N {current_pages} "
                    f"vs N-1 {prior_pages}"
                )
                parsed = run_copilot_page_group_extraction(
                    current_pdf,
                    prior_pdf,
                    prompt_path(settings, "document_compare_prompt"),
                    runs_dir / tag,
                    settings,
                    current_pages=current_pages,
                    prior_pages=prior_pages,
                    tag=tag,
                )
            else:
                page = current_pages[0]
                print(f"[document-review] Copilot current-only page {page}")
                parsed = run_copilot_single_page_extraction(
                    current_pdf,
                    prompt_path(settings, "document_current_only_prompt"),
                    runs_dir / tag,
                    settings,
                    page=page,
                    tag=tag,
                )
            parsed = _set_page_context(
                parsed,
                current_pages=current_pages,
                prior_pages=prior_pages,
                scope=scope,
            )
            write_json(comparison_path, parsed)
            comparisons.append(parsed)

        position_stats = refine_document_positions(
            comparisons,
            current_pdf=current_pdf,
            extraction_dir=extraction_dir,
            runs_dir=runs_dir,
            settings=settings,
            skip_copilot=args.skip_copilot,
            only_pages=(set(getattr(args, "position_page", None) or []) or None),
        )

    available_sources = {"bg"}
    if prior_pdf:
        available_sources.add("prior_fs")
    available_sources.update(args.available_source or [])
    rows = build_review_rows(
        comparisons,
        current_pdf=current_pdf,
        current_index=current_index,
        available_sources=available_sources,
        bg_path=args.bg.resolve() if args.bg else None,
        bg_paths={
            scope: path.resolve()
            for scope, path in (getattr(args, "bg_by_scope", None) or {}).items()
        },
        current_canonical=current_canonical,
    )
    workbook_path = output_dir / "Document_Review.xlsx"
    review_json_path = extraction_dir / "document_review.json"
    write_review_workbook(rows, workbook_path)
    write_review_json(rows, review_json_path)
    report = {
        "current_pdf": str(current_pdf),
        "prior_pdf": str(prior_pdf) if prior_pdf else None,
        "current_index": str(current_index_path),
        "prior_index": str(prior_index_path) if prior_pdf else None,
        "alignment": str(alignment_path),
        "comparison_count": len(comparisons),
        "review_mode": "canonical_structured" if use_canonical else "legacy_visual",
        "copilot_stats": copilot_stats,
        **position_stats,
        "review_row_count": len(rows),
        "document_review_xlsx": str(workbook_path),
        "document_review_json": str(review_json_path),
        "tickmarks": dict(
            sorted(
                {
                    tick: sum(1 for row in rows if row.get("tickmark") == tick)
                    for tick in {str(row.get("tickmark")) for row in rows}
                }.items()
            )
        ),
    }
    write_json(output_dir / "document_review_report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Copilot visual wording/annex review for a full financial report."
    )
    parser.add_argument("--current-pdf", type=Path, required=True)
    parser.add_argument("--prior-pdf", type=Path)
    parser.add_argument("--current-canonical", type=Path)
    parser.add_argument("--prior-canonical", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--bg", type=Path, help="Standardized BG used for annex support")
    parser.add_argument(
        "--settings",
        type=Path,
        default=PIPELINE_DIR / "config" / "settings.json",
    )
    parser.add_argument("--skip-copilot", action="store_true")
    parser.add_argument(
        "--position-page",
        action="append",
        type=int,
        help="Refine only this physical current-page position (repeatable).",
    )
    parser.add_argument(
        "--available-source",
        action="append",
        help="Additional evidence source available for this dossier.",
    )
    args = parser.parse_args()
    report = run_document_review(args)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
