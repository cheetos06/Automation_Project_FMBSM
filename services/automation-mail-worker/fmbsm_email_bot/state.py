from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class OutboundRateLimitExceeded(RuntimeError):
    pass


class MessageStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"messages": {}, "outbound_emails": []}
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict) or not isinstance(data.get("messages"), dict):
                return {"messages": {}, "outbound_emails": []}
            if not isinstance(data.get("outbound_emails"), list):
                data["outbound_emails"] = []
            return data
        except (OSError, json.JSONDecodeError):
            return {"messages": {}, "outbound_emails": []}

    def _save(self) -> None:
        self._data["updated_at"] = _utc_now()
        tmp_path = self.path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2, sort_keys=True)
        tmp_path.replace(self.path)

    def get(self, key: str) -> dict[str, Any] | None:
        value = self._data["messages"].get(key)
        return value if isinstance(value, dict) else None

    def is_terminal(self, key: str) -> bool:
        record = self.get(key)
        return bool(record and record.get("status") in {"processed", "failed"})

    def should_skip(self, key: str, *, processing_lock_seconds: int) -> tuple[bool, str]:
        record = self.get(key)
        if not record:
            return False, ""

        status = record.get("status")
        if status in {"processed", "failed"}:
            return True, str(status)
        if status == "interrupted":
            return False, "interrupted"
        if status != "processing":
            return False, ""

        started_at = _parse_utc(record.get("started_at"))
        if not started_at:
            return False, ""

        age = (datetime.now(timezone.utc) - started_at).total_seconds()
        if age < processing_lock_seconds:
            return True, "processing"
        return False, "stale_processing"

    def mark_processing(self, key: str, *, uid: str, subject: str, job_id: str) -> None:
        previous = self.get(key) or {}
        attempts = _safe_int(previous.get("attempts"), default=0) + 1
        self._data["messages"][key] = {
            "status": "processing",
            "uid": uid,
            "subject": subject,
            "job_id": job_id,
            "started_at": _utc_now(),
            "ack_sent_at": previous.get("ack_sent_at"),
            "attempts": attempts,
        }
        self._save()

    def mark_ack_sent(self, key: str) -> None:
        record = self._data["messages"].setdefault(key, {})
        record["ack_sent_at"] = _utc_now()
        self._save()

    def mark_finished(self, key: str, *, status: str, job_id: str, error: str | None = None) -> None:
        record = self._data["messages"].setdefault(key, {})
        record.update({"status": status, "job_id": job_id, "finished_at": _utc_now()})
        if error:
            record["error"] = error[:1000]
        self._save()

    def mark_interrupted_processing(self) -> int:
        count = 0
        for record in self._data["messages"].values():
            if isinstance(record, dict) and record.get("status") == "processing":
                record["status"] = "interrupted"
                record["interrupted_at"] = _utc_now()
                record["error"] = "Worker restarted while this job was processing"
                count += 1
        if count:
            self._save()
        return count

    def reserve_outbound_send(self, *, max_per_hour: int, max_per_day: int) -> None:
        now = datetime.now(timezone.utc)
        retained: list[str] = []
        hour_count = 0

        for value in self._data.get("outbound_emails", []):
            parsed = _parse_utc(value)
            if not parsed:
                continue
            age_seconds = (now - parsed).total_seconds()
            if age_seconds <= 24 * 60 * 60:
                retained.append(parsed.isoformat(timespec="seconds"))
            if age_seconds <= 60 * 60:
                hour_count += 1

        day_count = len(retained)
        if hour_count >= max_per_hour or day_count >= max_per_day:
            raise OutboundRateLimitExceeded(
                f"Outbound email rate limit reached: {hour_count}/{max_per_hour} this hour, "
                f"{day_count}/{max_per_day} today"
            )

        retained.append(now.isoformat(timespec="seconds"))
        self._data["outbound_emails"] = retained
        self._save()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_int(value: object, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
