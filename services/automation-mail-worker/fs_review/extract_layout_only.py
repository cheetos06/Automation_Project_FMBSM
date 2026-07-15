from __future__ import annotations

import argparse
from pathlib import Path

from copilot_extract import (
    load_settings,
    merge_layouts,
    normalize_layout,
    prompt_path,
    run_copilot_single_page_extraction,
    write_json,
)


PIPELINE_DIR = Path(__file__).resolve().parent


def _all_pdf_pages(pdf: Path) -> list[int]:
    import fitz

    document = fitz.open(pdf)
    try:
        return list(range(1, document.page_count + 1))
    finally:
        document.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract only FS tickmark positions with Copilot vision."
    )
    parser.add_argument(
        "--pdf",
        type=Path,
        default=PIPELINE_DIR.parent / "Examples" / "17779" / "Input" / "financial_statements_N.pdf",
        help="Unpointed financial statements PDF.",
    )
    parser.add_argument(
        "--pages",
        type=int,
        nargs="*",
        help="Optional physical PDF pages to extract one by one, 1-based.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PIPELINE_DIR.parent / "Examples" / "17779" / "Output" / "extraction" / "fs_tick_layout.json",
        help="Output layout JSON.",
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
    run_root = output.parent.parent / "copilot_runs" / "layout_only"
    pages = args.pages if args.pages else _all_pdf_pages(pdf)
    layouts = []
    for page in pages:
        print(f"[layout] extracting physical PDF page {page} with Copilot...")
        parsed = run_copilot_single_page_extraction(
            pdf,
            prompt_path(settings, "layout_prompt"),
            run_root / f"page_{page}",
            settings,
            page=page,
            tag="fs_tick_layout",
        )
        layout = normalize_layout(
            parsed,
            pdf_path=pdf,
            source=str(pdf),
            override_page=page,
        )
        print(f"[layout] page {page}: {len(layout.get('entries', []))} entries")
        layouts.append(layout)

    merged = merge_layouts(layouts, source=str(pdf))
    write_json(output, merged)
    print(f"[layout] wrote {len(merged.get('entries', []))} entries to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
