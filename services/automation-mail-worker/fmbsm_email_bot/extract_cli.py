from __future__ import annotations

import json
import sys
from pathlib import Path

from .extractor import create_excel_outputs


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: python -m fmbsm_email_bot.extract_cli manifest.json outputs.json", file=sys.stderr)
        return 2

    manifest_path = Path(sys.argv[1])
    outputs_path = Path(sys.argv[2])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    pdf_paths = [Path(value) for value in manifest["pdf_paths"]]
    output_dir = Path(manifest["output_dir"])
    job_id = str(manifest["job_id"])
    label_root = Path(manifest["label_root"]) if manifest.get("label_root") else None

    outputs = create_excel_outputs(pdf_paths, output_dir, job_id, label_root=label_root)
    outputs_path.write_text(
        json.dumps([str(path) for path in outputs], indent=2),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
