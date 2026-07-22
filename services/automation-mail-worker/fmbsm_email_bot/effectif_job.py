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


def run_effectif_job(
    *,
    pdf_paths: list[Path],
    output_dir: Path,
    job_id: str,
    label_root: Path,
    timeout_seconds: int,
    project_dir: Path,
    status_store: JobStatusStore,
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = output_dir / "effectif_manifest.json"
    result = output_dir / "effectif_outputs.json"
    manifest.write_text(
        json.dumps(
            {
                "pdf_paths": [str(path) for path in pdf_paths],
                "output_dir": str(output_dir),
                "job_id": job_id,
                "label_root": str(label_root),
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
        "effectif_extract.run_pipeline",
        str(manifest),
        str(result),
    ]
    status_store.update(
        job_id,
        kind="effectif_payroll",
        stage="pipeline_starting",
        message=f"Starting effectif extraction for {len(pdf_paths)} PDF(s)",
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
        LOGGER.info("Effectif job %s: %s", job_id, completed.stdout[-8000:].strip())
    if completed.stderr:
        LOGGER.warning("Effectif job %s stderr: %s", job_id, completed.stderr[-4000:].strip())
    if completed.returncode != 0:
        raise RuntimeError(
            f"Effectif pipeline exited with code {completed.returncode}: "
            f"{completed.stderr[-2000:].strip()}"
        )
    if not result.exists():
        raise RuntimeError("Effectif pipeline completed without an output manifest")
    outputs = [Path(value) for value in json.loads(result.read_text(encoding="utf-8"))]
    if not outputs or any(not path.exists() for path in outputs):
        raise RuntimeError("Effectif pipeline output workbook is missing")
    status_store.update(
        job_id,
        kind="effectif_payroll",
        stage="completed",
        message="Effectif/payroll workbook is ready",
        elapsed_seconds=round(elapsed, 1),
        result_file=outputs[0].name,
    )
    return outputs
