from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from copilot_extract import (
    load_settings,
    normalize_document_index,
    prompt_path,
    run_copilot_pdf_extraction,
    write_json,
)


PIPELINE_DIR = Path(__file__).resolve().parent


def extract_document_index(
    pdf: Path,
    output: Path,
    run_dir: Path,
    settings: dict[str, Any],
    *,
    tag: str,
) -> dict[str, Any]:
    parsed = run_copilot_pdf_extraction(
        pdf,
        prompt_path(settings, "document_index_prompt"),
        run_dir,
        settings,
        tag=tag,
    )
    normalized = normalize_document_index(parsed, pdf_path=pdf, source=str(pdf))
    write_json(output, normalized)
    return normalized


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Index every PDF page with Copilot vision."
    )
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--settings",
        type=Path,
        default=PIPELINE_DIR / "config" / "settings.json",
    )
    parser.add_argument("--tag", default="document_index")
    args = parser.parse_args()
    settings = load_settings(args.settings)
    output = args.output.resolve()
    data = extract_document_index(
        args.pdf.resolve(),
        output,
        output.parent / "copilot_runs" / args.tag,
        settings,
        tag=args.tag,
    )
    scopes = sorted({str(page.get("scope")) for page in data.get("pages", [])})
    print(
        f"Indexed {len(data.get('pages', []))} pages from {args.pdf.resolve()} "
        f"with scopes: {', '.join(scopes)}"
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
