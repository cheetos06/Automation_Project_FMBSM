from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Generic, TypeVar

from copilot_service.registry import CopilotRegistry

from copilot_extract import (
    CopilotAccount,
    is_copilot_rate_limit_error,
    load_copilot_accounts,
)


JobT = TypeVar("JobT")
ResultT = TypeVar("ResultT")


@dataclass(frozen=True)
class PoolRun(Generic[ResultT]):
    results: list[ResultT]
    account_stats: dict[str, dict[str, float | int]]
    elapsed_seconds: float


def _settings_for_account(settings: dict[str, Any], account: CopilotAccount) -> dict[str, Any]:
    selected = dict(settings)
    selected["copilot_account_name"] = account.name
    selected["copilot_account_names"] = [account.name]
    return selected


def run_account_pool(
    jobs: list[JobT],
    settings: dict[str, Any],
    operation: Callable[[JobT, dict[str, Any], str], ResultT],
) -> PoolRun[ResultT]:
    """Run independent calls through the durable, process-safe AWS account pool."""

    if not jobs:
        return PoolRun(results=[], account_stats={}, elapsed_seconds=0.0)
    accounts = load_copilot_accounts(settings)
    registry = CopilotRegistry.from_env()
    turn_limit = max(1, int(settings.get("copilot_turn_limit_per_account", 150)))
    cooldown_seconds = max(1.0, float(settings.get("copilot_cooldown_seconds", 3600)))
    window_seconds = max(60.0, float(settings.get("copilot_turn_window_seconds", 3600)))
    job_id = str(settings.get("_job_id") or "") or None
    operation_name = getattr(operation, "__name__", "copilot_call")

    pending: queue.Queue[tuple[int, JobT]] = queue.Queue()
    for index, job in enumerate(jobs):
        pending.put((index, job))
    results: list[ResultT | None] = [None] * len(jobs)
    stats: dict[str, dict[str, float | int]] = {
        account.name: {
            "jobs": 0,
            "seconds": 0.0,
            "turns_dispatched": 0,
            "rate_limits": 0,
            "requeued": 0,
            "cooldowns": 0,
            "cooldown_wait_seconds": 0.0,
        }
        for account in accounts
    }
    errors: list[BaseException] = []
    state_changed = threading.Condition()
    stop = threading.Event()
    remaining = len(jobs)

    def worker(account: CopilotAccount) -> None:
        nonlocal remaining
        account_settings = _settings_for_account(settings, account)
        while not stop.is_set():
            with state_changed:
                if remaining == 0:
                    return
            try:
                index, job = pending.get(timeout=0.5)
            except queue.Empty:
                continue

            allowed, wait_seconds, reason, turn_count = registry.reserve_turn(
                account.name,
                turn_limit=turn_limit,
                window_seconds=window_seconds,
                cooldown_seconds=cooldown_seconds,
                job_id=job_id,
                operation=operation_name,
            )
            if not allowed:
                pending.put((index, job))
                if reason in {"missing", "disabled"}:
                    with state_changed:
                        errors.append(RuntimeError(f"Copilot account {account.name} became {reason}"))
                        stop.set()
                        state_changed.notify_all()
                    return
                wait_started = time.perf_counter()
                sleep_for = min(max(wait_seconds, 0.25), 2.0)
                stop.wait(sleep_for)
                stats[account.name]["cooldown_wait_seconds"] = round(
                    float(stats[account.name]["cooldown_wait_seconds"])
                    + (time.perf_counter() - wait_started),
                    3,
                )
                continue

            stats[account.name]["turns_dispatched"] = int(stats[account.name]["turns_dispatched"]) + 1
            if reason == "turn_limit":
                stats[account.name]["cooldowns"] = int(stats[account.name]["cooldowns"]) + 1
                print(
                    f"[copilot-pool] {account.name} dispatched turn {turn_count}/{turn_limit}; "
                    f"cooldown starts after this call.",
                    flush=True,
                )
            else:
                print(
                    f"[copilot-pool] account={account.name} turn={turn_count}/{turn_limit} "
                    f"pending={pending.qsize()}",
                    flush=True,
                )

            started = time.perf_counter()
            try:
                result = operation(job, account_settings, account.name)
            except BaseException as exc:
                elapsed = time.perf_counter() - started
                stats[account.name]["seconds"] = round(float(stats[account.name]["seconds"]) + elapsed, 3)
                if is_copilot_rate_limit_error(exc):
                    registry.start_cooldown(
                        account.name,
                        seconds=cooldown_seconds,
                        reason="rate_limit",
                        error=str(exc),
                    )
                    stats[account.name]["rate_limits"] = int(stats[account.name]["rate_limits"]) + 1
                    stats[account.name]["requeued"] = int(stats[account.name]["requeued"]) + 1
                    stats[account.name]["cooldowns"] = int(stats[account.name]["cooldowns"]) + 1
                    pending.put((index, job))
                    print(
                        f"[copilot-pool] rate limit account={account.name}; request requeued "
                        f"cooldown={cooldown_seconds:.0f}s",
                        flush=True,
                    )
                    continue
                with state_changed:
                    errors.append(exc)
                    stop.set()
                    state_changed.notify_all()
                return

            elapsed = time.perf_counter() - started
            with state_changed:
                if results[index] is None:
                    results[index] = result
                    remaining -= 1
                    stats[account.name]["jobs"] = int(stats[account.name]["jobs"]) + 1
                stats[account.name]["seconds"] = round(float(stats[account.name]["seconds"]) + elapsed, 3)
                state_changed.notify_all()

    started = time.perf_counter()
    threads = [
        threading.Thread(target=worker, args=(account,), name=f"copilot-{account.name}", daemon=False)
        for account in accounts
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    if errors:
        raise errors[0]
    if any(result is None for result in results):
        raise RuntimeError("Copilot account pool ended before all jobs completed")
    return PoolRun(
        results=[result for result in results if result is not None],
        account_stats=stats,
        elapsed_seconds=time.perf_counter() - started,
    )
