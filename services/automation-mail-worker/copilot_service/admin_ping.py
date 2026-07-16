from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .job_status import JobStatusStore
from .registry import CopilotRegistry
from .token_refresh import ensure_account_fresh


class CopilotPingManager:
    """Run bounded, audited Copilot health prompts outside the HTTP request thread."""

    def __init__(self, registry: CopilotRegistry, status_store: JobStatusStore) -> None:
        self.registry = registry
        self.status_store = status_store
        self._start_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._active_job_id: str | None = None

    def start(self, account_ids: list[str]) -> dict[str, Any]:
        known = {account.account_id for account in self.registry.list_accounts(enabled_only=False)}
        selected = list(dict.fromkeys(account_ids))
        missing = [account_id for account_id in selected if account_id not in known]
        if not selected or missing:
            raise ValueError(f"Unknown or empty Copilot account selection: {missing}")
        job_id = "copilot-ping-" + uuid.uuid4().hex[:12]
        with self._state_lock:
            if self._active_job_id is not None:
                raise RuntimeError(
                    f"Copilot health test {self._active_job_id} is already running"
                )
            self._active_job_id = job_id
        try:
            status = self.status_store.update(
                job_id,
                kind="copilot_ping",
                stage="queued",
                message=f"Queued health test for {len(selected)} Copilot account(s).",
                account_ids=selected,
                results=[],
                progress={"completed": 0, "total": len(selected)},
            )
            thread = threading.Thread(
                target=self._run,
                args=(job_id, selected),
                name=job_id,
                daemon=True,
            )
            thread.start()
        except Exception:
            with self._state_lock:
                if self._active_job_id == job_id:
                    self._active_job_id = None
            raise
        return status

    def recent(self, *, limit: int = 20) -> list[dict[str, Any]]:
        return [
            item
            for item in self.status_store.recent(limit=max(limit * 3, 30))
            if item.get("kind") == "copilot_ping"
        ][:limit]

    def _run(self, job_id: str, account_ids: list[str]) -> None:
        with self._start_lock:
            work_root = self.registry.data_dir / "admin-tests" / job_id
            results: list[dict[str, Any]] = []
            try:
                work_root.mkdir(parents=True, exist_ok=True)
                image_path = self._create_test_image(work_root)
                self._update(
                    job_id,
                    stage="running",
                    message="Copilot health test started.",
                    account_ids=account_ids,
                    results=results,
                    completed=0,
                    total=len(account_ids),
                )
                for index, account_id in enumerate(account_ids, 1):
                    account = self.registry.get_account(account_id)
                    if account is None:
                        results.append(
                            {"account_id": account_id, "username": "unknown", "ok": False, "error": "missing"}
                        )
                        continue
                    self._update(
                        job_id,
                        stage="running",
                        message=f"Testing {account.username} ({index}/{len(account_ids)})...",
                        account_ids=account_ids,
                        results=results,
                        completed=index - 1,
                        total=len(account_ids),
                    )
                    started = time.perf_counter()
                    row: dict[str, Any] = {
                        "account_id": account.account_id,
                        "username": account.username,
                        "ok": False,
                    }
                    try:
                        fresh = ensure_account_fresh(
                            self.registry,
                            account,
                            minimum_remaining_seconds=600,
                            timeout_seconds=45,
                        )
                        allowed, wait_seconds, reason, turn_count = self.registry.reserve_turn(
                            fresh.account_id,
                            turn_limit=150,
                            window_seconds=3600,
                            cooldown_seconds=3600,
                            job_id=job_id,
                            operation="admin_copilot_ping",
                        )
                        if not allowed:
                            raise RuntimeError(
                                f"Copilot account unavailable: {reason or 'cooldown'}; "
                                f"retry in {wait_seconds:.0f}s"
                            )
                        from copilot_runtime.runtime import CopilotRuntime

                        runtime = CopilotRuntime(
                            profile_dir=fresh.session_path,
                            session_dir=fresh.session_path,
                            output_dir=work_root / fresh.account_id,
                            timeout_seconds=120,
                            cleanup=True,
                            save_private_frames=False,
                        )
                        parsed = runtime.extract(
                            image_path,
                            prompt=(
                                "This is an authorized FMBSM system health check. "
                                "Return exactly this JSON object and nothing else: "
                                '{"pool_health":"ok"}'
                            ),
                            dpi=96,
                            cleanup=True,
                        )
                        if not (
                            isinstance(parsed, dict)
                            and str(parsed.get("pool_health") or "").lower() == "ok"
                        ):
                            raise RuntimeError(
                                "Unexpected response: "
                                + json.dumps(parsed, ensure_ascii=True)[:300]
                            )
                        row.update({"ok": True, "turn": turn_count})
                    except Exception as exc:
                        row.update(
                            {
                                "error_type": type(exc).__name__,
                                "error": str(exc)[:700],
                            }
                        )
                    row["elapsed_seconds"] = round(time.perf_counter() - started, 2)
                    results.append(row)
                    self._update(
                        job_id,
                        stage="running",
                        message=(
                            f"{account.username}: {'working' if row['ok'] else 'failed'} "
                            f"({index}/{len(account_ids)})."
                        ),
                        account_ids=account_ids,
                        results=results,
                        completed=index,
                        total=len(account_ids),
                    )
                passed = sum(1 for result in results if result.get("ok"))
                self._update(
                    job_id,
                    stage="complete" if passed == len(results) else "complete_with_failures",
                    message=f"Copilot health test finished: {passed}/{len(results)} working.",
                    account_ids=account_ids,
                    results=results,
                    completed=len(account_ids),
                    total=len(account_ids),
                    finished_at=time.time(),
                )
            except Exception as exc:
                self.status_store.update(
                    job_id,
                    kind="copilot_ping",
                    stage="failed",
                    message=f"Copilot health test failed: {exc}",
                    account_ids=account_ids,
                    results=results,
                    error=str(exc)[:1000],
                    finished_at=time.time(),
                )
            finally:
                shutil.rmtree(work_root, ignore_errors=True)
                with self._state_lock:
                    if self._active_job_id == job_id:
                        self._active_job_id = None

    def _update(
        self,
        job_id: str,
        *,
        stage: str,
        message: str,
        account_ids: list[str],
        results: list[dict[str, Any]],
        completed: int,
        total: int,
        **fields: Any,
    ) -> None:
        self.status_store.update(
            job_id,
            kind="copilot_ping",
            stage=stage,
            message=message,
            account_ids=account_ids,
            results=list(results),
            progress={"completed": completed, "total": total},
            **fields,
        )

    @staticmethod
    def _create_test_image(work_root: Path) -> Path:
        import fitz

        path = work_root / "copilot-health-check.png"
        document = fitz.open()
        try:
            page = document.new_page(width=600, height=800)
            page.draw_rect(fitz.Rect(40, 40, 560, 760), color=(0, 0.25, 0.4), width=4)
            page.insert_text(
                (80, 120),
                "FMBSM COPILOT POOL HEALTH CHECK",
                fontsize=20,
                color=(0, 0, 0),
            )
            page.insert_text(
                (80, 170),
                "Automated diagnostic document - no business data",
                fontsize=13,
                color=(0, 0, 0),
            )
            page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False).save(str(path))
        finally:
            document.close()
        return path
