from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from copilot_service.job_status import JobStatusStore


LOGGER = logging.getLogger(__name__)


def run_balance_job(
    *,
    workbook_paths: list[Path],
    output_dir: Path,
    job_id: str,
    timeout_seconds: int,
    project_dir: Path,
    status_store: JobStatusStore,
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = output_dir / "balance_manifest.json"
    result = output_dir / "balance_outputs.json"
    manifest.write_text(
        json.dumps(
            {
                "workbook_paths": [str(path) for path in workbook_paths],
                "output_dir": str(output_dir),
                "job_id": job_id,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"
    environment["FMBSM_JOB_ID"] = job_id
    command = [
        sys.executable,
        "-u",
        "-m",
        "balance_cleaner.run_pipeline",
        str(manifest),
        str(result),
    ]
    status_store.update(
        job_id,
        kind="balance_cleaner",
        stage="pipeline_starting",
        message=f"Starting balance cleaning for {len(workbook_paths)} workbook(s)",
    )
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=project_dir,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
    )
    elapsed = time.monotonic() - started
    if completed.stdout:
        LOGGER.info("Balance job %s: %s", job_id, completed.stdout[-12000:].strip())
    if completed.stderr:
        LOGGER.warning("Balance job %s stderr: %s", job_id, completed.stderr[-4000:].strip())
    if completed.returncode != 0:
        raise RuntimeError(
            f"Balance cleaner exited with code {completed.returncode}: "
            f"{completed.stderr[-2000:].strip()}"
        )
    if not result.exists():
        raise RuntimeError("Balance cleaner completed without an output manifest")
    outputs = [Path(value) for value in json.loads(result.read_text(encoding="utf-8"))]
    if not outputs or any(not path.exists() for path in outputs):
        raise RuntimeError("Balance cleaner output workbook is missing")
    status_store.update(
        job_id,
        kind="balance_cleaner",
        stage="completed",
        message="Cleaned balance workbook is ready",
        elapsed_seconds=round(elapsed, 1),
        result_files=[path.name for path in outputs],
    )
    return outputs
