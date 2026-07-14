from __future__ import annotations

import argparse
from pathlib import Path

from copilot_extract import (
    load_settings,
    normalize_fs_extraction,
    prompt_path,
    run_copilot_pdf_extraction,
    run_copilot_single_page_extraction,
    write_json,
)


PIPELINE_DIR = Path(__file__).resolve().parent


def _merge_extractions(items: list[dict], *, source: str) -> dict:
    lines = []
    notes = []
    for item in items:
        lines.extend(item.get("lines", []))
        notes.extend(item.get("quality_notes", []))
    return {
        "source": source,
        "extraction_method": "Copilot vision over rendered PDF page images",
        "lines": lines,
        "quality_notes": notes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract only FS lines/amounts with Copilot vision."
    )
    parser.add_argument("--pdf", type=Path, required=True, help="Source FS PDF.")
    parser.add_argument(
        "--output", type=Path, required=True, help="Output FS extraction JSON."
    )
    parser.add_argument(
        "--prompt-key",
        choices=["fs_extract_prompt", "prior_extract_prompt"],
        default="fs_extract_prompt",
        help="Prompt configured in settings.json.",
    )
    parser.add_argument(
        "--pages",
        type=int,
        nargs="*",
        help="Optional physical PDF pages to extract one by one, 1-based.",
    )
    parser.add_argument(
        "--settings",
        type=Path,
        default=PIPELINE_DIR / "config" / "settings.json",
        help="Pipeline settings JSON.",
    )
    args = parser.parse_args()

    settings = load_settings(args.settings)
    pdf = args.pdf.resolve()
    output = args.output.resolve()
    prompt = prompt_path(settings, args.prompt_key)
    run_root = output.parent.parent / "copilot_runs" / output.stem

    if args.pages:
        extractions = []
        for page in args.pages:
            print(f"[fs] extracting physical PDF page {page} with Copilot...")
            parsed = run_copilot_single_page_extraction(
                pdf,
                prompt,
                run_root / f"page_{page}",
                settings,
                page=page,
                tag=output.stem,
            )
            normalized = normalize_fs_extraction(
                parsed,
                source=str(pdf),
                override_page=page,
            )
            print(f"[fs] page {page}: {len(normalized.get('lines', []))} lines")
            extractions.append(normalized)
        result = _merge_extractions(extractions, source=str(pdf))
    else:
        print("[fs] extracting full PDF with Copilot...")
        parsed = run_copilot_pdf_extraction(
            pdf,
            prompt,
            run_root,
            settings,
            tag=output.stem,
        )
        result = normalize_fs_extraction(parsed, source=str(pdf))

    write_json(output, result)
    print(f"[fs] wrote {len(result.get('lines', []))} lines to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
