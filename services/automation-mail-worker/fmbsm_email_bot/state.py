from __future__ import annotations

import json
import os
import statistics
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


class OutboundRateLimitExceeded(RuntimeError):
    pass


class MessageStore:
    """Atomic, thread-safe persistence for the mail queue and delivery limits."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
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
        tmp_path = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(5):
            try:
                tmp_path.replace(self.path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.01 * (attempt + 1))

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._data["messages"].get(key)
            return dict(value) if isinstance(value, dict) else None

    def is_terminal(self, key: str) -> bool:
        record = self.get(key)
        return bool(record and record.get("status") in {"processed", "failed", "rejected"})

    def should_skip(self, key: str, *, processing_lock_seconds: int) -> tuple[bool, str]:
        record = self.get(key)
        if not record:
            return False, ""

        status = str(record.get("status") or "")
        if status in {"processed", "failed", "rejected", "queued"}:
            return True, status
        if status != "processing":
            return False, status

        started_at = _parse_utc(record.get("started_at"))
        if not started_at:
            return False, "stale_processing"
        age = (datetime.now(timezone.utc) - started_at).total_seconds()
        if age < processing_lock_seconds:
            return True, "processing"
        return False, "stale_processing"

    def mark_queued(
        self,
        key: str,
        *,
        uid: str,
        mailbox: str,
        subject: str,
        job_id: str,
        job_kind: str,
        raw_path: Path,
    ) -> None:
        with self._lock:
            previous = self._record(key)
            self._data["messages"][key] = {
                **previous,
                "status": "queued",
                "uid": uid,
                "mailbox": mailbox,
                "subject": subject,
                "job_id": job_id,
                "job_kind": job_kind,
                "raw_path": str(raw_path),
                "queued_at": previous.get("queued_at") or _utc_now(),
                "attempts": _safe_int(previous.get("attempts"), default=0),
            }
            self._save()

    def mark_processing(
        self,
        key: str,
        *,
        uid: str,
        subject: str,
        job_id: str,
        job_kind: str | None = None,
    ) -> None:
        with self._lock:
            previous = self._record(key)
            attempts = _safe_int(previous.get("attempts"), default=0) + 1
            now = _utc_now()
            self._data["messages"][key] = {
                **previous,
                "status": "processing",
                "uid": uid,
                "subject": subject,
                "job_id": job_id,
                "job_kind": job_kind or previous.get("job_kind"),
                "started_at": now,
                "processing_started_at": now,
                "attempts": attempts,
            }
            self._save()

    def mark_ack_sent(self, key: str, *, queue_position: int | None = None) -> None:
        with self._lock:
            record = self._record(key, create=True)
            record["ack_sent_at"] = _utc_now()
            if queue_position is not None:
                record["ack_queue_position"] = queue_position
            self._save()

    def mark_start_sent(self, key: str) -> None:
        with self._lock:
            self._record(key, create=True)["start_sent_at"] = _utc_now()
            self._save()

    def mark_retry_notice_sent(self, key: str) -> None:
        with self._lock:
            self._record(key, create=True)["retry_notice_sent_at"] = _utc_now()
            self._save()

    def mark_result_sent(self, key: str) -> None:
        with self._lock:
            self._record(key, create=True)["result_sent_at"] = _utc_now()
            self._save()

    def mark_retry(self, key: str, *, error: str, delay_seconds: int) -> None:
        with self._lock:
            record = self._record(key, create=True)
            record.update(
                {
                    "status": "queued",
                    "last_error": error[:1000],
                    "retry_queued_at": _utc_now(),
                    "next_attempt_at": (
                        datetime.now(timezone.utc) + timedelta(seconds=max(0, delay_seconds))
                    ).isoformat(timespec="seconds"),
                }
            )
            self._save()

    def mark_finished(
        self,
        key: str,
        *,
        status: str,
        job_id: str,
        error: str | None = None,
    ) -> None:
        with self._lock:
            record = self._record(key, create=True)
            now = datetime.now(timezone.utc)
            started = _parse_utc(record.get("processing_started_at"))
            record.update(
                {
                    "status": status,
                    "job_id": job_id,
                    "finished_at": now.isoformat(timespec="seconds"),
                }
            )
            if started:
                record["duration_seconds"] = round(max(0.0, (now - started).total_seconds()), 3)
            if error:
                record["error"] = error[:1000]
            self._save()

    def mark_interrupted_processing(self) -> int:
        """Put interrupted jobs back in FIFO order for automatic replay."""

        with self._lock:
            count = 0
            for record in self._data["messages"].values():
                if isinstance(record, dict) and record.get("status") == "processing":
                    record["status"] = "queued"
                    record["interrupted_at"] = _utc_now()
                    record["next_attempt_at"] = _utc_now()
                    record["last_error"] = "Worker restarted while this job was processing"
                    record["recovery_count"] = _safe_int(record.get("recovery_count"), default=0) + 1
                    count += 1
            if count:
                self._save()
            return count

    def queued_count(self) -> int:
        with self._lock:
            return sum(
                1
                for record in self._data["messages"].values()
                if isinstance(record, dict) and record.get("status") == "queued"
            )

    def active_count(self) -> int:
        with self._lock:
            return sum(
                1
                for record in self._data["messages"].values()
                if isinstance(record, dict) and record.get("status") == "processing"
            )

    def next_queued(self) -> tuple[str, dict[str, Any]] | None:
        now = datetime.now(timezone.utc)
        with self._lock:
            candidates = []
            for key, value in self._data["messages"].items():
                if not isinstance(value, dict) or value.get("status") != "queued":
                    continue
                due = _parse_utc(value.get("next_attempt_at"))
                if due and due > now:
                    continue
                candidates.append((str(value.get("queued_at") or ""), str(key), dict(value)))
            if not candidates:
                return None
            _, key, record = min(candidates, key=lambda item: (item[0], item[1]))
            return key, record

    def queue_snapshot(
        self,
        key: str,
        *,
        default_seconds: Mapping[str, int],
    ) -> dict[str, int]:
        """Return one-based position and conservative start/completion estimates."""

        now = datetime.now(timezone.utc)
        with self._lock:
            records = [
                dict(value)
                for value in self._data["messages"].values()
                if isinstance(value, dict) and value.get("status") in {"processing", "queued"}
            ]
            target = self._record(key)
            estimates = self._duration_estimates(default_seconds)
        records.sort(
            key=lambda value: (
                0 if value.get("status") == "processing" else 1,
                str(value.get("queued_at") or ""),
                str(value.get("job_id") or ""),
            )
        )
        target_job_id = str(target.get("job_id") or "")
        position = next(
            (
                index
                for index, value in enumerate(records, start=1)
                if str(value.get("job_id") or "") == target_job_id
            ),
            max(1, len(records)),
        )
        start_seconds = 0.0
        for value in records[: max(0, position - 1)]:
            estimate = float(estimates.get(str(value.get("job_kind") or ""), 300))
            if value.get("status") == "processing":
                started = _parse_utc(value.get("processing_started_at"))
                elapsed = (now - started).total_seconds() if started else 0.0
                estimate = max(30.0, estimate - max(0.0, elapsed))
            start_seconds += estimate
        own_seconds = float(estimates.get(str(target.get("job_kind") or ""), 300))
        return {
            "position": position,
            "total": len(records),
            "estimated_start_seconds": int(round(start_seconds)),
            "estimated_completion_seconds": int(round(start_seconds + own_seconds)),
        }

    def reserve_outbound_send(self, *, max_per_hour: int, max_per_day: int) -> None:
        with self._lock:
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

    def _duration_estimates(self, defaults: Mapping[str, int]) -> dict[str, int]:
        samples: dict[str, list[float]] = {str(key): [] for key in defaults}
        terminal = sorted(
            (
                value
                for value in self._data["messages"].values()
                if isinstance(value, dict) and value.get("status") == "processed"
            ),
            key=lambda value: str(value.get("finished_at") or ""),
            reverse=True,
        )
        for value in terminal:
            kind = str(value.get("job_kind") or "")
            duration = value.get("duration_seconds")
            if kind in samples and isinstance(duration, (int, float)) and duration > 0:
                if len(samples[kind]) < 20:
                    samples[kind].append(float(duration))
        return {
            # Historical samples refine the estimate upward, but never let a
            # handful of unusually fast jobs undercut the configured service
            # time. Use the slowest recent successful job because queue
            # acknowledgements should be conservative promises, not medians.
            kind: max(
                30,
                int(default),
                int(round(max(values))) if values else 0,
            )
            for kind, default in defaults.items()
            for values in (samples.get(kind, []),)
        }

    def _record(self, key: str, *, create: bool = False) -> dict[str, Any]:
        if create:
            value = self._data["messages"].setdefault(key, {})
        else:
            value = self._data["messages"].get(key, {})
        return value if isinstance(value, dict) else {}


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
