from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Iterable

from copilot_service.job_status import JobStatusStore


LOGGER = logging.getLogger(__name__)
RESULT_NAMES = {
    "BG_Mapped.xlsx",
    "FS_Mapped.xlsx",
    "Document_Review.xlsx",
    "financial_statements_N_ticked_by_pipeline.pdf",
    "mapper_audit.md",
    "pipeline_report.json",
    "document_review_report.json",
}


def prepare_ticket(files: Iterable[Path], job_dir: Path) -> Path:
    candidates = [path for path in files if path.is_file()]
    pdfs = [path for path in candidates if path.suffix.lower() == ".pdf"]
    workbooks = [path for path in candidates if path.suffix.lower() in {".xlsx", ".xlsm"}]
    if not pdfs:
        raise RuntimeError("FS review requires a current-year financial-statements PDF")
    if not workbooks:
        raise RuntimeError("FS review requires bg_standardized.xlsx")

    prior_candidates = [path for path in pdfs if _looks_prior(path.name)]
    current_candidates = [path for path in pdfs if path not in prior_candidates]
    if len(current_candidates) != 1:
        names = ", ".join(path.name for path in pdfs)
        raise RuntimeError(
            "Could not identify exactly one current-year PDF. Name inputs "
            f"financial_statements_N.pdf and financial_statements_N_1.pdf. Received: {names}"
        )
    if len(prior_candidates) > 1:
        raise RuntimeError("More than one N-1/prior-year PDF was attached")
    bg_candidates = [path for path in workbooks if "bg" in path.stem.lower()]
    if len(bg_candidates) != 1:
        names = ", ".join(path.name for path in workbooks)
        raise RuntimeError(f"Could not identify exactly one BG workbook. Received: {names}")

    input_dir = job_dir / "ticket" / "Input"
    input_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(current_candidates[0], input_dir / "financial_statements_N.pdf")
    if prior_candidates:
        shutil.copy2(prior_candidates[0], input_dir / "financial_statements_N_1.pdf")
    shutil.copy2(bg_candidates[0], input_dir / "bg_standardized.xlsx")
    return input_dir.parent


def run_fs_review(
    *,
    ticket_dir: Path,
    output_dir: Path,
    job_id: str,
    year: int,
    timeout_seconds: int,
    project_dir: Path,
    status_store: JobStatusStore,
    maximum_result_bytes: int,
) -> list[Path]:
    pipeline = project_dir / "fs_review" / "run_pipeline.py"
    settings = project_dir / "fs_review" / "config" / "settings.json"
    if not pipeline.exists() or not settings.exists():
        raise RuntimeError("The deployed FS review framework is incomplete")
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "fs-review.log"
    command = [
        sys.executable,
        "-u",
        str(pipeline),
        "--ticket",
        str(ticket_dir),
        "--settings",
        str(settings),
        "--year",
        str(year),
    ]
    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(project_dir), str(project_dir / "fs_review"), environment.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    environment["FMBSM_JOB_ID"] = job_id
    status_store.update(
        job_id,
        kind="fs_review",
        stage="pipeline_starting",
        message="Launching the isolated FS review pipeline",
        command=command,
        year=year,
    )
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=project_dir,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        start_new_session=os.name != "nt",
    )
    lines: queue.Queue[str | None] = queue.Queue()

    def read_output() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line.rstrip("\r\n"))
        lines.put(None)

    reader = threading.Thread(target=read_output, name=f"fs-log-{job_id}", daemon=True)
    reader.start()
    last_heartbeat = 0.0
    reader_finished = False
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(f"command={command!r}\n")
        while process.poll() is None or not reader_finished:
            elapsed = time.monotonic() - started
            if elapsed > timeout_seconds:
                _terminate_process_tree(process)
                status_store.update(
                    job_id,
                    kind="fs_review",
                    stage="timed_out",
                    message=f"FS review exceeded the {timeout_seconds}s timeout",
                    elapsed_seconds=round(elapsed, 1),
                    finished_at=time.time(),
                )
                raise TimeoutError(f"FS review timed out after {timeout_seconds} seconds")
            try:
                line = lines.get(timeout=1)
            except queue.Empty:
                line = ""
            if line is None:
                reader_finished = True
            elif line:
                LOGGER.info("FS job %s: %s", job_id, line)
                log_file.write(line + "\n")
                log_file.flush()
                stage = _stage_from_line(line)
                status_store.update(
                    job_id,
                    kind="fs_review",
                    stage=stage,
                    message=line,
                    elapsed_seconds=round(elapsed, 1),
                    process_id=process.pid,
                )
                last_heartbeat = time.monotonic()
            if time.monotonic() - last_heartbeat >= 20:
                status_store.update(
                    job_id,
                    kind="fs_review",
                    stage="pipeline_running",
                    message="Pipeline is active; waiting for the current Copilot/local step",
                    elapsed_seconds=round(elapsed, 1),
                    process_id=process.pid,
                )
                last_heartbeat = time.monotonic()
        return_code = process.wait(timeout=10)
    reader.join(timeout=2)
    elapsed = time.monotonic() - started
    if return_code != 0:
        tail = _tail(log_path, 40)
        status_store.update(
            job_id,
            kind="fs_review",
            stage="failed",
            message=f"Pipeline exited with code {return_code}",
            error=tail,
            elapsed_seconds=round(elapsed, 1),
            finished_at=time.time(),
        )
        raise RuntimeError(f"FS pipeline exited with code {return_code}. Last output:\n{tail[-3000:]}")

    pipeline_output = ticket_dir / "Output"
    report = pipeline_output / "pipeline_report.json"
    if not report.exists():
        raise RuntimeError("FS pipeline completed without pipeline_report.json")
    try:
        report_payload = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"FS pipeline report is unreadable: {exc}") from exc
    result_zip = output_dir / f"FS_Review_{job_id}.zip"
    _write_results_zip(pipeline_output, result_zip, log_path)
    if result_zip.stat().st_size > maximum_result_bytes:
        raise RuntimeError(
            f"FS result ZIP is {result_zip.stat().st_size} bytes, exceeding email limit {maximum_result_bytes}"
        )
    status_store.update(
        job_id,
        kind="fs_review",
        stage="completed",
        message="FS review completed and result archive is ready",
        elapsed_seconds=round(elapsed, 1),
        finished_at=time.time(),
        copilot_call_count=report_payload.get("copilot_call_count"),
        result_file=result_zip.name,
        result_bytes=result_zip.stat().st_size,
    )
    return [result_zip]


def _looks_prior(filename: str) -> bool:
    normalized = (
        filename.lower()
        .replace("−", "-")
        .replace("–", "-")
        .replace("—", "-")
        .replace(" ", "_")
    )
    return any(marker in normalized for marker in ("n_1", "n-1", "nminus1", "prior", "previous"))


def _stage_from_line(line: str) -> str:
    lowered = line.lower()
    if "canonical" in lowered or "copilot" in lowered:
        return "copilot_processing"
    if "document" in lowered and "review" in lowered:
        return "document_review"
    if "mapped" in lowered or "mapping" in lowered:
        return "mapping"
    if "tick" in lowered:
        return "tickmarking"
    return "pipeline_running"


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
        )
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _write_results_zip(pipeline_output: Path, destination: Path, log_path: Path) -> None:
    selected = [
        path
        for path in pipeline_output.rglob("*")
        if path.is_file() and (path.name in RESULT_NAMES or path.name.endswith("_Mapped.xlsx"))
    ]
    if not selected:
        raise RuntimeError("The FS pipeline generated no user-facing result files")
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(selected):
            archive.write(path, path.relative_to(pipeline_output).as_posix())
        archive.write(log_path, "diagnostics/fs-review.log")


def _tail(path: Path, count: int) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-count:])
    except OSError:
        return ""
