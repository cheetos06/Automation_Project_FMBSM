from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
PROJECT_DIR = ROOT.parent
FS_REVIEW_DIR = PROJECT_DIR / "fs_review"
if str(FS_REVIEW_DIR) not in sys.path:
    sys.path.insert(0, str(FS_REVIEW_DIR))

from copilot_extract import load_copilot_account  # noqa: E402
from copilot_pool import run_account_pool  # noqa: E402

from .render import Screenshot, WorkbookCapture, capture_workbook  # noqa: E402


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _safe_stem(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return cleaned[:80] or "balance"


def chunk_screenshots(
    screenshots: tuple[Screenshot, ...],
    batch_size: int,
    max_estimated_records: int = 0,
) -> list[tuple[Screenshot, ...]]:
    # Reserve one of Copilot's 30 image slots for the frozen 30-row
    # worksheet-header/structure context repeated on later requests.
    effective = min(29, max(1, int(batch_size)))
    record_limit = max(0, int(max_estimated_records))
    batches: list[tuple[Screenshot, ...]] = []
    current: list[Screenshot] = []
    estimated_records = 0
    for screenshot in screenshots:
        estimate = max(0, int(getattr(screenshot, "estimated_records", 0)))
        if current and (
            len(current) >= effective
            or (
                record_limit > 0
                and estimated_records + estimate > record_limit
            )
        ):
            batches.append(tuple(current))
            current = []
            estimated_records = 0
        current.append(screenshot)
        estimated_records += estimate
    if current:
        batches.append(tuple(current))
    return batches


def _batch_prompt(
    base_prompt: str,
    capture: WorkbookCapture,
    batch: tuple[Screenshot, ...],
    batch_index: int,
    batch_count: int,
    context_images: tuple[Screenshot, ...] = (),
) -> str:
    target_start = len(context_images) + 1
    labels = "; ".join(
        f"image {index}=Excel rows {item.row_start}-{item.row_end}"
        for index, item in enumerate(batch, start=target_start)
    )
    context_note = ""
    if context_images:
        context_labels = "; ".join(
            f"image {index}=Excel rows {item.row_start}-{item.row_end}"
            for index, item in enumerate(context_images, start=1)
        )
        context_note = (
            f"Attached {context_labels} are HEADER/STRUCTURE CONTEXT ONLY. Use them "
            "together to identify the account, description, debit, credit, and "
            "final-balance columns and their sign convention. Do not extract or "
            "repeat any account from those context images. "
        )
    return (
        f"{base_prompt.strip()}\n\n"
        f"Batch context: this is batch {batch_index} of {batch_count} for worksheet "
        f"{capture.sheet_name!r}. The batches are consecutive and never overlap. "
        f"{context_note}"
        "Extract only rows visible in the attached images and never repeat an account "
        "from a different batch. "
        f"Target image order: {labels}."
    )


def _extract_records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        for key in ("accounts", "records", "data", "result"):
            if isinstance(value.get(key), list):
                value = value[key]
                break
    if not isinstance(value, list):
        raise RuntimeError("Copilot response is not a JSON array of accounts")

    records: list[dict[str, Any]] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise RuntimeError(f"Copilot account item {index} is not a JSON object")
        account_number = _account_number(item.get("account_number"))
        description = str(item.get("account_description") or "").strip()
        saldo = _number(item.get("saldo"))
        if not account_number or not description or saldo is None:
            raise RuntimeError(
                f"Copilot account item {index} is missing account_number, "
                "account_description, or numeric saldo"
            )
        records.append(
            {
                "account_number": account_number,
                "account_description": description,
                "saldo": saldo,
            }
        )
    return records


def _account_number(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    token = str(value).strip().replace("\u00a0", "").replace(" ", "").replace("'", "")
    negative_parentheses = token.startswith("(") and token.endswith(")")
    token = token.strip("()")
    if "," in token and "." in token:
        token = (
            token.replace(".", "").replace(",", ".")
            if token.rfind(",") > token.rfind(".")
            else token.replace(",", "")
        )
    elif "," in token:
        token = token.replace(",", ".")
    try:
        number = float(token)
    except ValueError:
        return None
    if not math.isfinite(number):
        return None
    return -abs(number) if negative_parentheses else number


def _write_workbook(path: Path, records: list[dict[str, Any]]) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Cleaned balance"
    sheet.append(["account_number", "account_description", "saldo"])
    for item in records:
        sheet.append(
            [item["account_number"], item["account_description"], item["saldo"]]
        )

    header_fill = PatternFill("solid", fgColor="1F4E78")
    header_font = Font(color="FFFFFF", bold=True)
    for cell in sheet[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for cell in sheet["A"][1:]:
        cell.number_format = "@"
    for cell in sheet["C"][1:]:
        cell.number_format = '#,##0.00;[Red]-#,##0.00'
    sheet.column_dimensions["A"].width = 22
    sheet.column_dimensions["B"].width = 65
    sheet.column_dimensions["C"].width = 24
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:C{max(1, sheet.max_row)}"
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


def _capture_manifest(capture: WorkbookCapture) -> dict[str, Any]:
    def screenshot_payload(item: Screenshot) -> dict[str, Any]:
        return {
            "path": str(item.path),
            "row_start": item.row_start,
            "row_end": item.row_end,
            "populated_rows": list(item.populated_rows),
            "first_column": item.first_column,
            "last_column": item.last_column,
            "populated_columns": list(item.populated_columns),
            "estimated_records": item.estimated_records,
        }

    return {
        "source_path": str(capture.source_path),
        "workbook_path": str(capture.workbook_path),
        "sheet_name": capture.sheet_name,
        "last_populated_row": capture.last_populated_row,
        "first_populated_column": capture.first_populated_column,
        "last_populated_column": capture.last_populated_column,
        "populated_columns": list(capture.populated_columns),
        "header_context": (
            screenshot_payload(capture.header_context)
            if capture.header_context is not None
            else None
        ),
        "screenshots": [screenshot_payload(item) for item in capture.screenshots],
    }


def _process_one(
    source: Path,
    workbook_index: int,
    manifest: dict[str, Any],
    settings: dict[str, Any],
    base_prompt: str,
) -> Path:
    output_dir = Path(manifest["output_dir"])
    job_id = str(manifest["job_id"])
    work_dir = output_dir / "work" / f"{workbook_index:02d}_{_safe_stem(source.stem)}"
    print(f"[balance-cleaner] rendering {source.name}", flush=True)
    capture = capture_workbook(
        source,
        work_dir,
        rows_per_image=int(settings.get("rows_per_image", 100)),
        image_max_width=int(settings.get("image_max_width", 12000)),
        libreoffice_bin=str(settings.get("libreoffice_bin", "libreoffice")),
        last_row_limit=(
            int(settings["_last_row_limit"])
            if settings.get("_last_row_limit") is not None
            else None
        ),
    )
    (work_dir / "capture_manifest.json").write_text(
        json.dumps(_capture_manifest(capture), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    batches = chunk_screenshots(
        capture.screenshots,
        int(settings.get("batch_size", 30)),
        int(settings.get("max_estimated_records_per_request", 0)),
    )
    print(
        f"[balance-cleaner] sheet={capture.sheet_name!r} last_row={capture.last_populated_row} "
        f"screenshots={len(capture.screenshots)} batches={len(batches)}",
        flush=True,
    )

    jobs = [
        {"batch": batch_index, "screenshots": batch}
        for batch_index, batch in enumerate(batches, start=1)
    ]

    def operation(job, selected: dict[str, Any], account: str) -> dict[str, Any]:
        batch_index = int(job["batch"])
        batch = job["screenshots"]
        # Every screenshot uses the same frozen columns and up to 30 populated
        # rows. Every request receives only the pre-table header strip, never
        # account rows borrowed from another target screenshot.
        context_images = (
            (capture.header_context,)
            if capture.header_context is not None
            else ()
        )
        request_images = (*context_images, *batch)
        if len(request_images) > 30:
            raise RuntimeError("Copilot balance request exceeds the 30-image limit")
        account_config = load_copilot_account(selected)
        from copilot_runtime.runtime import CopilotRuntime

        run_dir = (
            work_dir
            / "copilot_runs"
            / f"batch_{batch_index:03d}"
            / _safe_stem(account)
        )
        runtime = CopilotRuntime(
            profile_dir=account_config.profile_dir,
            session_dir=account_config.session_dir,
            output_dir=run_dir,
            timeout_seconds=int(selected.get("timeout_seconds", 1200)),
            cleanup=bool(selected.get("cleanup_rendered_images", True)),
            save_private_frames=False,
        )
        started = time.perf_counter()
        metadata = runtime.extract(
            input_path=[item.path for item in request_images],
            page=1,
            prompt=_batch_prompt(
                base_prompt,
                capture,
                batch,
                batch_index,
                len(batches),
                context_images,
            ),
            combine_inputs=True,
            return_metadata=True,
        )
        if not isinstance(metadata, dict):
            raise RuntimeError("Copilot did not return response metadata")
        records = _extract_records(metadata.get("parsed"))
        print(
            f"[balance-cleaner] batch={batch_index}/{len(batches)} "
            f"account={account} records={len(records)}",
            flush=True,
        )
        return {
            "batch": batch_index,
            "account": account,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "records": records,
        }

    pool = run_account_pool(jobs, settings, operation)
    results = list(pool.results)
    by_batch = {
        int(result["batch"]): result
        for result in results
    }

    records: list[dict[str, Any]] = []
    for batch_index in range(1, len(batches) + 1):
        records.extend(by_batch[batch_index]["records"])
    diagnostics = {
        "capture": _capture_manifest(capture),
        "record_count": len(records),
        "batch_results": results,
        "account_stats": pool.account_stats,
        "copilot_calls": len(results),
        "elapsed_seconds": round(pool.elapsed_seconds, 3),
    }
    (work_dir / "balance_extraction.json").write_text(
        json.dumps(diagnostics, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    suffix = "" if len(manifest["workbook_paths"]) == 1 else f"_{workbook_index:02d}_{_safe_stem(source.stem)}"
    workbook_path = output_dir / f"balance_cleaned_{job_id}{suffix}.xlsx"
    _write_workbook(workbook_path, records)
    # Re-open once so corrupt/partial output is caught before email delivery.
    verification = load_workbook(workbook_path, read_only=True, data_only=True)
    verification.close()
    print(
        f"[balance-cleaner] completed source={source.name} records={len(records)} "
        f"output={workbook_path.name}",
        flush=True,
    )
    return workbook_path


def main() -> int:
    if len(sys.argv) != 3:
        print(
            "Usage: python -m balance_cleaner.run_pipeline manifest.json outputs.json",
            file=sys.stderr,
        )
        return 2
    manifest_path, outputs_path = map(Path, sys.argv[1:3])
    env_file = os.getenv("ENV_FILE")
    load_dotenv(
        dotenv_path=Path(env_file) if env_file else PROJECT_DIR / ".env",
        encoding="utf-8-sig",
    )
    manifest = _read_json(manifest_path)
    settings = _read_json(ROOT / "config" / "settings.json")
    settings["_job_id"] = str(manifest["job_id"])
    prompt_path = ROOT / str(settings["prompt_file"])
    base_prompt = prompt_path.read_text(encoding="utf-8-sig")
    sources = [Path(value) for value in manifest["workbook_paths"]]
    if not sources:
        raise RuntimeError("No Excel workbooks were supplied")
    outputs = [
        _process_one(source, index, manifest, settings, base_prompt)
        for index, source in enumerate(sources, start=1)
    ]
    outputs_path.write_text(
        json.dumps([str(path) for path in outputs], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
