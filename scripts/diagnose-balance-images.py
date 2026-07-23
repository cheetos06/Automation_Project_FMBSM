from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
DEPLOYED_SERVICE = ROOT
REPOSITORY_SERVICE = ROOT / "services" / "automation-mail-worker"
SERVICE = (
    DEPLOYED_SERVICE
    if (DEPLOYED_SERVICE / "balance_cleaner").is_dir()
    else REPOSITORY_SERVICE
)
FS_REVIEW = SERVICE / "fs_review"
for candidate in (str(SERVICE), str(FS_REVIEW)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from balance_cleaner.run_pipeline import _process_one  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Exercise the production balance image/Copilot path on a bounded row range."
    )
    parser.add_argument("workbook", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--rows-per-image", type=int, default=30)
    parser.add_argument("--max-row", type=int, default=300)
    parser.add_argument("--group-size", type=int, default=30)
    parser.add_argument("--job-id", default="balance-image-diagnostic")
    args = parser.parse_args()

    env_file = os.getenv("ENV_FILE")
    load_dotenv(
        dotenv_path=Path(env_file) if env_file else SERVICE / ".env",
        encoding="utf-8-sig",
    )
    settings_path = SERVICE / "balance_cleaner" / "config" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["_job_id"] = args.job_id
    settings["_last_row_limit"] = args.max_row
    settings["rows_per_image"] = args.rows_per_image
    settings["batch_size"] = args.group_size
    prompt_path = SERVICE / "balance_cleaner" / str(settings["prompt_file"])
    workbook = args.workbook.resolve()
    if not workbook.is_file():
        raise FileNotFoundError(workbook)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    result = _process_one(
        workbook,
        1,
        {
            "output_dir": str(output),
            "job_id": args.job_id,
            "workbook_paths": [str(workbook)],
        },
        settings,
        prompt_path.read_text(encoding="utf-8-sig"),
    )
    print(f"DIAGNOSTIC_OUTPUT={result}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
