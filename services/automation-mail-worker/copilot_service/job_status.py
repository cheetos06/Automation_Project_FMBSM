from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any


class JobStatusStore:
    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def update(self, job_id: str, *, stage: str, message: str, **fields: Any) -> dict[str, Any]:
        now = time.time()
        status_path = self.directory / f"{job_id}.json"
        with self._lock:
            current: dict[str, Any] = {}
            try:
                current = json.loads(status_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
            current.update(fields)
            current.update(
                {
                    "job_id": job_id,
                    "stage": stage,
                    "message": message[:2000],
                    "updated_at": now,
                }
            )
            current.setdefault("started_at", now)
            event = {
                "at": now,
                "job_id": job_id,
                "stage": stage,
                "message": message[:2000],
                **{key: value for key, value in fields.items() if key not in {"output", "error"}},
            }
            temporary = status_path.with_name(
                f".{status_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            temporary.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(status_path)
            with (self.directory / f"{job_id}.events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        return current

    def get(self, job_id: str) -> dict[str, Any] | None:
        status_path = self.directory / f"{job_id}.json"
        with self._lock:
            try:
                value = json.loads(status_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
        return value if isinstance(value, dict) else None

    def recent(self, *, limit: int = 20) -> list[dict[str, Any]]:
        paths = sorted(
            self.directory.glob("*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:limit]
        result: list[dict[str, Any]] = []
        for path in paths:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                result.append(value)
        return result
