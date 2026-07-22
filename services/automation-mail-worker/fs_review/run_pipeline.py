from __future__ import annotations

import argparse
import json
import re
import shutil
import time
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

from copilot_extract import (
    load_settings,
    merge_layouts,
    normalize_fs_extraction,
    normalize_layout,
    prompt_path,
    read_json,
    run_copilot_pdf_extraction,
    run_copilot_single_page_extraction,
    write_json,
)
from align_layout_to_fs import align_layout
from align_prior_to_fs import align_prior
from canonical_extraction import (
    derive_pipeline_artifacts,
    extract_canonical_documents,
)
from fs_mapping import write_fs_mapping
from map_bg_to_fs import load_bg, process_ticket, write_audit_report, write_output
from run_document_review import run_document_review
from tick_fs_pdf import tick_pdf


PIPELINE_DIR = Path(__file__).resolve().parent


def _copy_seed(seed: Path | None, destination: Path) -> bool:
    if seed is None:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(seed.resolve(), destination)
    return True


def _input_dir(ticket: Path, settings: dict[str, Any]) -> Path:
    configured = ticket / str(settings.get("input_dir_name", "Input"))
    return configured if configured.exists() else ticket


def _all_pdf_pages(pdf: Path) -> list[int]:
    import fitz

    document = fitz.open(pdf)
    try:
        return list(range(1, document.page_count + 1))
    finally:
        document.close()


def _scope(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = re.sub(r"[^a-z]+", " ", text)
    if "consolid" in text:
        return "consolidated"
    if any(word in text for word in ("annual", "social", "individuel")):
        return "annual"
    return ""


def _items(data: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(data, dict) and isinstance(data.get(key), list):
        return [item for item in data[key] if isinstance(item, dict)]
    return []


def _statement_scopes(fs_path: Path) -> list[str]:
    data = read_json(fs_path)
    scopes = {_scope(item.get("scope")) for item in _items(data, "lines")}
    scopes.discard("")
    return sorted(scopes or {"annual"})


def _canonical_reporting_year(path: Path) -> int | None:
    if not path.exists():
        return None
    payload = read_json(path)
    years: Counter[int] = Counter()
    for page in payload.get("pages", []) if isinstance(payload, dict) else []:
        if not isinstance(page, dict):
            continue
        match = re.search(r"\b(20\d{2})\b", str(page.get("period_end") or ""))
        if match:
            years[int(match.group(1))] += 1
    return years.most_common(1)[0][0] if years else None


def _quality_gate(mapping_reports: dict[str, dict[str, Any]]) -> dict[str, Any]:
    reasons: list[str] = []
    metrics: dict[str, Any] = {}
    review_required = False
    for scope, report in mapping_reports.items():
        stats = report.get("stats") or {}
        bg_total = int(stats.get("nonzero_rows") or 0)
        bg_matched = int(stats.get("matched_nonzero_rows") or 0)
        fs_total = int(stats.get("nonzero_fs_leaf_lines") or 0)
        fs_justified = int(stats.get("justified_nonzero_fs_lines") or 0)
        bg_coverage = bg_matched / bg_total if bg_total else 0.0
        fs_coverage = fs_justified / fs_total if fs_total else 0.0
        metrics[scope] = {
            "bg_rows": bg_total,
            "bg_matched": bg_matched,
            "bg_coverage": round(bg_coverage, 4),
            "fs_leaf_lines": fs_total,
            "fs_justified": fs_justified,
            "fs_coverage": round(fs_coverage, 4),
        }
        if not bg_total:
            review_required = True
            reasons.append(f"{scope}: no nonzero BG rows were available for reconciliation")
        elif bg_coverage < 0.8:
            review_required = True
            reasons.append(f"{scope}: only {bg_matched}/{bg_total} nonzero BG rows mapped")
        elif bg_coverage < 0.95:
            reasons.append(f"{scope}: BG mapping coverage is {bg_coverage:.1%}")
        if not fs_total:
            review_required = True
            reasons.append(f"{scope}: no nonzero detailed FS lines were extracted")
        elif fs_coverage < 0.5:
            review_required = True
            reasons.append(f"{scope}: only {fs_justified}/{fs_total} FS leaf lines were justified")
        elif fs_coverage < 0.75:
            reasons.append(f"{scope}: FS justification coverage is {fs_coverage:.1%}")
        for warning in stats.get("quality_warnings", []) or []:
            reasons.append(f"{scope}: {warning}")
    status = "review_required" if review_required else ("pass_with_warnings" if reasons else "pass")
    return {"status": status, "reasons": reasons, "metrics": metrics}


def _write_scoped_json(
    source: Path,
    destination: Path,
    *,
    scope: str,
    item_key: str,
    allow_unscoped: bool,
) -> None:
    data = read_json(source)
    source_items = _items(data, item_key)
    selected = [
        item
        for item in source_items
        if _scope(item.get("scope")) == scope
        or (allow_unscoped and not _scope(item.get("scope")))
    ]
    result = dict(data) if isinstance(data, dict) else {}
    result[item_key] = selected
    result["scope"] = scope
    write_json(destination, result)


def _write_no_primary_mapping(
    *,
    input_dir: Path,
    bg_path: Path,
    bg_mapped_path: Path,
    fs_mapped_path: Path,
    audit_path: Path,
    reporting_year: int,
) -> dict[str, Any]:
    """Produce explicit review artifacts for an annex-only document."""

    bg_rows = load_bg(bg_path)
    write_output(bg_rows, bg_mapped_path)
    write_fs_mapping([], fs_mapped_path)
    nonzero_rows = sum(abs(row.amount) > 0.01 for row in bg_rows)
    stats: dict[str, Any] = {
        "variant": "official-pcg",
        "rows": len(bg_rows),
        "matched": 0,
        "unmatched": len(bg_rows),
        "classified": 0,
        "contextual": 0,
        "contextual_resolved": 0,
        "non_fs": 0,
        "group_matches": 0,
        "combined_group_matches": 0,
        "gross_up_matches": 0,
        "semantic_matches": 0,
        "gross_up_adjustments": [],
        "row_matches": 0,
        "fs_lines": 0,
        "fs_leaf_lines": 0,
        "justified_fs_lines": 0,
        "reconciliation": [],
        "category_differences": [],
        "regime": "not_applicable",
        "ticket": input_dir.parent.name if input_dir.name.lower() == "input" else input_dir.name,
        "input_dir": str(input_dir),
        "output": str(bg_mapped_path),
        "fs_output": str(fs_mapped_path),
        "matched_nonzero_rows": 0,
        "nonzero_rows": nonzero_rows,
        "justified_nonzero_fs_lines": 0,
        "nonzero_fs_leaf_lines": 0,
        "quality_warnings": [
            "No statutory Bilan/CPC pages were found; the document appears to be "
            "annex-only, so BG-to-FS reconciliation was not attempted"
        ],
        "reporting_year": reporting_year,
        "result_reconciliation": None,
        "audit_report": str(audit_path),
    }
    write_audit_report(stats, audit_path)
    return stats


def _scope_bg_paths(
    input_dir: Path,
    settings: dict[str, Any],
    scopes: list[str],
) -> dict[str, Path]:
    default = input_dir / str(settings.get("bg_file_name", "bg_standardized.xlsx"))
    if len(scopes) == 1:
        if not default.exists():
            raise FileNotFoundError(f"Missing BG file: {default}")
        return {scopes[0]: default}

    configured = settings.get("scope_bg_files") or {}
    candidate_names = {
        "annual": (
            "bg_standardized_annual.xlsx",
            "bg_standardized_social.xlsx",
            "bg_annual.xlsx",
            "bg_social.xlsx",
        ),
        "consolidated": (
            "bg_standardized_consolidated.xlsx",
            "bg_consolidated.xlsx",
        ),
    }
    result: dict[str, Path] = {}
    missing: list[str] = []
    for scope in scopes:
        configured_name = configured.get(scope) if isinstance(configured, dict) else None
        candidates = []
        if configured_name:
            candidates.append(input_dir / str(configured_name))
        candidates.extend(input_dir / name for name in candidate_names.get(scope, ()))
        match = next((path for path in candidates if path.exists()), None)
        if match is None:
            missing.append(scope)
        else:
            result[scope] = match
    if missing:
        raise ValueError(
            "Multiple annual/consolidated statement sets were detected. "
            "Separate BG files are required and the generic bg_standardized.xlsx "
            "will not be guessed. Configure scope_bg_files in settings.json or use "
            "scope-specific filenames. Missing: "
            + ", ".join(missing)
        )
    return result


def _merge_fs_extractions(items: list[dict[str, Any]], *, source: str) -> dict[str, Any]:
    lines: list[dict[str, Any]] = []
    notes: list[Any] = []
    for item in items:
        lines.extend(item.get("lines", []))
        notes.extend(item.get("quality_notes", []))
    return {
        "source": source,
        "extraction_method": "Copilot vision over rendered PDF page images",
        "lines": lines,
        "quality_notes": notes,
    }


def _extract_fs_json(
    pdf: Path,
    output: Path,
    run_dir: Path,
    settings: dict[str, Any],
    *,
    prompt_key: str,
    pages_key: str,
    tag: str,
) -> None:
    pages = settings.get(pages_key) or []
    if pages:
        extractions: list[dict[str, Any]] = []
        for page in pages:
            page_number = int(page)
            print(f"[pipeline] {tag}: Copilot FS extraction page {page_number}")
            parsed = run_copilot_single_page_extraction(
                pdf,
                prompt_path(settings, prompt_key),
                run_dir / f"page_{page_number}",
                settings,
                page=page_number,
                tag=tag,
            )
            extractions.append(
                normalize_fs_extraction(
                    parsed,
                    source=str(pdf),
                    override_page=page_number,
                )
            )
        write_json(output, _merge_fs_extractions(extractions, source=str(pdf)))
        return

    print(f"[pipeline] {tag}: Copilot FS extraction full PDF")
    parsed = run_copilot_pdf_extraction(
        pdf,
        prompt_path(settings, prompt_key),
        run_dir,
        settings,
        tag=tag,
    )
    write_json(output, normalize_fs_extraction(parsed, source=str(pdf)))


def _extract_layout_json(
    pdf: Path,
    output: Path,
    run_dir: Path,
    settings: dict[str, Any],
) -> None:
    configured_pages = settings.get("layout_pages") or []
    pages = [int(page) for page in configured_pages] if configured_pages else _all_pdf_pages(pdf)
    layouts: list[dict[str, Any]] = []
    for page_number in pages:
        print(f"[pipeline] layout: Copilot position extraction page {page_number}")
        parsed = run_copilot_single_page_extraction(
            pdf,
            prompt_path(settings, "layout_prompt"),
            run_dir / f"page_{page_number}",
            settings,
            page=page_number,
            tag="fs_tick_layout",
        )
        layout = normalize_layout(
            parsed,
            pdf_path=pdf,
            source=str(pdf),
            override_page=page_number,
        )
        if layout.get("entries"):
            layouts.append(layout)
        else:
            layouts.append(
                {
                    "coordinate_system": "PDF points, origin at top-left",
                    "source": str(pdf),
                    "entries": [],
                    "quality_notes": [
                        f"No visible Bilan/CPC amount boxes found on physical page {page_number}."
                    ],
                }
            )
    write_json(output, merge_layouts(layouts, source=str(pdf)))


def run_pipeline(args: argparse.Namespace) -> dict[str, Any]:
    pipeline_started = time.perf_counter()
    settings = load_settings(args.settings)
    ticket = args.ticket.resolve()
    input_dir = _input_dir(ticket, settings)
    output_dir = ticket / str(settings.get("output_dir_name", "Output"))
    extraction_dir = output_dir / "extraction"
    copilot_run_dir = output_dir / "copilot_runs"

    fs_pdf = input_dir / str(settings.get("source_pdf_name", "financial_statements_N.pdf"))
    prior_pdf = input_dir / str(settings.get("prior_pdf_name", "financial_statements_N_1.pdf"))
    bg_path = input_dir / str(settings.get("bg_file_name", "bg_standardized.xlsx"))
    if not fs_pdf.exists():
        raise FileNotFoundError(f"Missing source FS PDF: {fs_pdf}")

    fs_json = extraction_dir / "fs_extract_N.json"
    prior_json = extraction_dir / "fs_extract_N_1.json"
    layout_json = extraction_dir / "fs_tick_layout.json"
    current_canonical = extraction_dir / "canonical_N.json"
    prior_canonical = extraction_dir / "canonical_N_1.json"
    current_index = extraction_dir / "document_index_N.json"
    prior_index = extraction_dir / "document_index_N_1.json"

    copied_fs = _copy_seed(args.seed_fs_json, fs_json)
    copied_prior = _copy_seed(args.seed_prior_json, prior_json)
    copied_layout = _copy_seed(args.seed_layout_json, layout_json)

    seed_mode = copied_fs or copied_prior or copied_layout
    canonical_stats: dict[str, Any] = {}
    reuse_canonical = bool(getattr(args, "reuse_canonical", False))
    if (
        not args.skip_copilot
        and reuse_canonical
        and current_canonical.exists()
        and (not prior_pdf.exists() or prior_canonical.exists())
    ):
        canonical_stats = {
            "vision_calls": 0,
            "elapsed_seconds": 0.0,
            "reused_after_downstream_failure": True,
            "documents": {
                "N": str(current_canonical),
                **({"N_1": str(prior_canonical)} if prior_pdf.exists() else {}),
            },
        }
        derive_pipeline_artifacts(
            current_canonical,
            pdf_path=fs_pdf,
            fs_path=fs_json,
            layout_path=layout_json,
            index_path=current_index,
        )
        if prior_pdf.exists():
            derive_pipeline_artifacts(
                prior_canonical,
                pdf_path=prior_pdf,
                fs_path=prior_json,
                layout_path=None,
                index_path=prior_index,
            )
            write_json(prior_json, align_prior(prior_json, fs_json))
        write_json(layout_json, align_layout(layout_json, fs_json))
    elif not args.skip_copilot and not seed_mode:
        documents = {"N": fs_pdf}
        outputs = {"N": current_canonical}
        if prior_pdf.exists():
            documents["N_1"] = prior_pdf
            outputs["N_1"] = prior_canonical
        canonical_stats = extract_canonical_documents(
            documents,
            output_paths=outputs,
            runs_dir=copilot_run_dir / "canonical_pages",
            settings=settings,
        )
        derive_pipeline_artifacts(
            current_canonical,
            pdf_path=fs_pdf,
            fs_path=fs_json,
            layout_path=layout_json,
            index_path=current_index,
        )
        if prior_pdf.exists():
            derive_pipeline_artifacts(
                prior_canonical,
                pdf_path=prior_pdf,
                fs_path=prior_json,
                layout_path=None,
                index_path=prior_index,
            )
            write_json(prior_json, align_prior(prior_json, fs_json))
        write_json(layout_json, align_layout(layout_json, fs_json))
    elif not args.skip_copilot:
        if not copied_fs:
            _extract_fs_json(
                fs_pdf,
                fs_json,
                copilot_run_dir / "fs_extract_N",
                settings,
                prompt_key="fs_extract_prompt",
                pages_key="fs_pages",
                tag="fs_extract_N",
            )
        if prior_pdf.exists() and not copied_prior:
            _extract_fs_json(
                prior_pdf,
                prior_json,
                copilot_run_dir / "fs_extract_N_1",
                settings,
                prompt_key="prior_extract_prompt",
                pages_key="prior_pages",
                tag="fs_extract_N_1",
            )
            write_json(prior_json, align_prior(prior_json, fs_json))
        if not copied_layout:
            _extract_layout_json(
                fs_pdf,
                layout_json,
                copilot_run_dir / "fs_tick_layout",
                settings,
            )
            write_json(layout_json, align_layout(layout_json, fs_json))
    elif current_canonical.exists():
        derive_pipeline_artifacts(
            current_canonical,
            pdf_path=fs_pdf,
            fs_path=fs_json,
            layout_path=layout_json,
            index_path=current_index,
        )
        if prior_pdf.exists() and prior_canonical.exists():
            derive_pipeline_artifacts(
                prior_canonical,
                pdf_path=prior_pdf,
                fs_path=prior_json,
                layout_path=None,
                index_path=prior_index,
            )
            write_json(prior_json, align_prior(prior_json, fs_json))
        write_json(layout_json, align_layout(layout_json, fs_json))

    required = [fs_json, layout_json]
    missing = [path for path in required if not path.exists()]
    if missing:
        missing_text = "\n".join(str(path) for path in missing)
        raise FileNotFoundError(
            "Missing extraction files. Run without --skip-copilot or provide seeds:\n"
            f"{missing_text}"
        )

    scopes = _statement_scopes(fs_json)
    detected_year = _canonical_reporting_year(current_canonical)
    if detected_year is not None and detected_year != args.year:
        raise ValueError(
            f"Subject/config reporting year {args.year} conflicts with the visually detected "
            f"current reporting year {detected_year}. Resend with year={detected_year}."
        )
    bg_by_scope = _scope_bg_paths(input_dir, settings, scopes)
    mapping_reports: dict[str, dict[str, Any]] = {}
    fs_mapping_paths: list[Path] = []
    allow_unscoped = len(scopes) == 1
    for scope in scopes:
        scope_output = output_dir if len(scopes) == 1 else output_dir / scope
        scope_extraction = extraction_dir if len(scopes) == 1 else extraction_dir / scope
        scope_output.mkdir(parents=True, exist_ok=True)
        scope_extraction.mkdir(parents=True, exist_ok=True)
        scoped_fs = fs_json
        scoped_layout = layout_json
        scoped_prior = prior_json if prior_json.exists() else None
        if len(scopes) > 1:
            scoped_fs = scope_extraction / "fs_extract_N.json"
            scoped_layout = scope_extraction / "fs_tick_layout.json"
            _write_scoped_json(
                fs_json,
                scoped_fs,
                scope=scope,
                item_key="lines",
                allow_unscoped=False,
            )
            _write_scoped_json(
                layout_json,
                scoped_layout,
                scope=scope,
                item_key="entries",
                allow_unscoped=False,
            )
            if prior_json.exists():
                scoped_prior = scope_extraction / "fs_extract_N_1.json"
                _write_scoped_json(
                    prior_json,
                    scoped_prior,
                    scope=scope,
                    item_key="lines",
                    allow_unscoped=False,
                )
        bg_mapped_path = scope_output / "BG_Mapped.xlsx"
        fs_mapped_path = scope_output / "FS_Mapped.xlsx"
        audit_path = scope_output / "mapper_audit.md"
        if not _items(read_json(scoped_fs), "lines"):
            scope_stats = _write_no_primary_mapping(
                input_dir=input_dir,
                bg_path=bg_by_scope[scope],
                bg_mapped_path=bg_mapped_path,
                fs_mapped_path=fs_mapped_path,
                audit_path=audit_path,
                reporting_year=args.year,
            )
        else:
            scope_stats = process_ticket(
                input_dir,
                output=bg_mapped_path,
                fs_override=scoped_fs,
                reporting_year=args.year,
                audit_report=audit_path,
                fs_output=fs_mapped_path,
                layout_json=scoped_layout,
                prior_fs_json=scoped_prior,
                bg_override=bg_by_scope[scope],
            )
        mapping_reports[scope] = {
            "bg": str(bg_by_scope[scope]),
            "bg_mapped": str(bg_mapped_path),
            "fs_mapped": str(fs_mapped_path),
            "audit_report": str(audit_path),
            "stats": scope_stats,
        }
        fs_mapping_paths.append(fs_mapped_path)

    if len(scopes) == 1:
        only_scope = scopes[0]
        mapping = mapping_reports[only_scope]
        bg_mapped = Path(mapping["bg_mapped"])
        fs_mapped: Path | list[Path] = Path(mapping["fs_mapped"])
        audit_report = Path(mapping["audit_report"])
        stats: dict[str, Any] = mapping["stats"]
    else:
        bg_mapped = None
        fs_mapped = fs_mapping_paths
        audit_report = None
        stats = {"by_scope": {scope: item["stats"] for scope, item in mapping_reports.items()}}

    ticked_pdf = output_dir / "financial_statements_N_ticked_by_pipeline.pdf"
    document_review_report = None
    review_mapping = None
    if not args.no_document_review:
        document_review_report = run_document_review(
            argparse.Namespace(
                current_pdf=fs_pdf,
                prior_pdf=prior_pdf if prior_pdf.exists() else None,
                current_canonical=current_canonical if current_canonical.exists() else None,
                prior_canonical=prior_canonical if prior_canonical.exists() else None,
                bg=bg_path if len(scopes) == 1 else None,
                bg_by_scope=bg_by_scope,
                output_dir=output_dir,
                settings=args.settings,
                skip_copilot=args.skip_copilot,
                available_source=[],
            )
        )
        review_mapping = output_dir / "Document_Review.xlsx"
    tick_count = None
    if not args.no_tick_pdf:
        tick_count = tick_pdf(
            fs_pdf,
            fs_mapped,
            ticked_pdf,
            include_legend=bool(settings.get("include_pdf_legend", True)),
            review_mapping=review_mapping,
        )

    quality_gate = _quality_gate(mapping_reports)
    report = {
        "ticket": str(ticket),
        "input_dir": str(input_dir),
        "fs_pdf": str(fs_pdf),
        "bg": str(bg_path) if len(scopes) == 1 else None,
        "bg_by_scope": {scope: str(path) for scope, path in bg_by_scope.items()},
        "statement_scopes": scopes,
        "fs_extract": str(fs_json),
        "prior_extract": str(prior_json) if prior_json.exists() else None,
        "layout": str(layout_json),
        "canonical_current": str(current_canonical)
        if current_canonical.exists()
        else None,
        "canonical_prior": str(prior_canonical) if prior_canonical.exists() else None,
        "canonical_extraction": canonical_stats,
        "bg_mapped": str(bg_mapped) if bg_mapped else None,
        "fs_mapped": str(fs_mapped) if isinstance(fs_mapped, Path) else None,
        "audit_report": str(audit_report) if audit_report else None,
        "mapping_by_scope": mapping_reports,
        "ticked_pdf": str(ticked_pdf) if tick_count is not None else None,
        "tick_count": tick_count,
        "document_review": document_review_report,
        "mapper_stats": stats,
        "reporting_year": args.year,
        "detected_reporting_year": detected_year,
        "quality_gate": quality_gate,
        "copilot_call_count": int(canonical_stats.get("vision_calls", 0))
        + int(
            ((document_review_report or {}).get("copilot_stats") or {}).get(
                "semantic_calls", 0
            )
        ),
        "elapsed_seconds": round(time.perf_counter() - pipeline_started, 3),
    }
    write_json(output_dir / "pipeline_report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract FS with Copilot vision, map BG to FS, and add tickmarks."
    )
    parser.add_argument(
        "--ticket",
        type=Path,
        default=PIPELINE_DIR.parent / "Examples" / "17779",
        help="Ticket folder, usually containing an Input subfolder.",
    )
    parser.add_argument(
        "--settings",
        type=Path,
        default=PIPELINE_DIR / "config" / "settings.json",
        help="Pipeline settings JSON.",
    )
    parser.add_argument("--year", type=int, default=2025, help="FS reporting year.")
    parser.add_argument(
        "--skip-copilot",
        action="store_true",
        help="Do not call Copilot; require seed JSON/layout files or existing outputs.",
    )
    parser.add_argument(
        "--reuse-canonical",
        action="store_true",
        help=(
            "Reuse completed canonical page records after a downstream failure, "
            "while still running missing semantic Copilot review calls."
        ),
    )
    parser.add_argument("--seed-fs-json", type=Path, help="Existing visual FS JSON.")
    parser.add_argument("--seed-prior-json", type=Path, help="Existing visual N-1 JSON.")
    parser.add_argument("--seed-layout-json", type=Path, help="Existing visual layout JSON.")
    parser.add_argument(
        "--no-tick-pdf",
        action="store_true",
        help="Stop after mapping and do not create the ticked PDF.",
    )
    parser.add_argument(
        "--no-document-review",
        action="store_true",
        help="Run only Bilan/CPC mapping and skip wording/annex review.",
    )
    args = parser.parse_args()
    report = run_pipeline(args)
    stats = report["mapper_stats"]
    if "by_scope" in stats:
        for scope, scope_stats in stats["by_scope"].items():
            print(
                f"{Path(report['ticket']).name} [{scope}]: BG mapped "
                f"{scope_stats['matched_nonzero_rows']}/{scope_stats['nonzero_rows']} "
                f"nonzero rows, FS justified "
                f"{scope_stats['justified_nonzero_fs_lines']}/"
                f"{scope_stats['nonzero_fs_leaf_lines']} nonzero leaf lines."
            )
    else:
        print(
            f"{Path(report['ticket']).name}: BG mapped "
            f"{stats['matched_nonzero_rows']}/{stats['nonzero_rows']} nonzero rows, "
            f"FS justified {stats['justified_nonzero_fs_lines']}/"
            f"{stats['nonzero_fs_leaf_lines']} nonzero leaf lines."
        )
    if report.get("tick_count") is not None:
        print(f"Added {report['tick_count']} tickmarks: {report['ticked_pdf']}")
    print(f"Report: {report['ticket']}\\Output\\pipeline_report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
