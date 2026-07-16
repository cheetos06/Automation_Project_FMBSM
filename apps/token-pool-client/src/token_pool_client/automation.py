from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .storage import application_dir


WORK_ACCOUNT_DOMAINS = frozenset({"forvismazars.com", "mazars.fr"})
DEFAULT_REFRESH_TIMES = ("04:45", "09:45")
DEFAULT_UPDATE_INTERVAL_SECONDS = 60 * 60
MAX_SCHEDULE_CATCHUP = timedelta(hours=24)
STARTUP_VALUE_NAME = "FMBSM Token Pool Client"


class SingleInstance:
    """Keep one tray process and ask it to show itself on a second launch."""

    def __init__(self, on_second_launch) -> None:
        self.on_second_launch = on_second_launch
        self._handle = 0
        self._closing = False
        self._thread: threading.Thread | None = None

    def acquire(self) -> bool:
        if sys.platform != "win32":
            return True
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateEventW.restype = wintypes.HANDLE
        kernel32.SetEvent.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.SetLastError(0)
        handle = kernel32.CreateEventW(None, False, False, r"Local\FMBSMTokenPoolClientShow")
        if not handle:
            return True
        if kernel32.GetLastError() == 183:
            kernel32.SetEvent(handle)
            kernel32.CloseHandle(handle)
            return False
        self._handle = handle
        self._thread = threading.Thread(target=self._wait, name="token-pool-single-instance", daemon=True)
        self._thread.start()
        return True

    def _wait(self) -> None:
        import ctypes

        while self._handle and ctypes.windll.kernel32.WaitForSingleObject(self._handle, 0xFFFFFFFF) == 0:
            if self._closing:
                return
            self.on_second_launch()

    def close(self) -> None:
        if sys.platform != "win32" or not self._handle:
            return
        import ctypes

        self._closing = True
        ctypes.windll.kernel32.SetEvent(self._handle)
        if self._thread is not None:
            self._thread.join(timeout=2)
        ctypes.windll.kernel32.CloseHandle(self._handle)
        self._handle = 0


def is_automatic_work_account(username: str) -> bool:
    value = username.strip().lower()
    if "@" not in value:
        return False
    return value.rsplit("@", 1)[1] in WORK_ACCOUNT_DOMAINS


def configured_refresh_times(value: str | None = None) -> tuple[str, ...]:
    raw = value if value is not None else os.getenv("TOKEN_POOL_REFRESH_TIMES", "")
    candidates = [item.strip() for item in raw.split(",") if item.strip()] if raw else list(DEFAULT_REFRESH_TIMES)
    parsed: list[str] = []
    for item in candidates:
        try:
            moment = datetime.strptime(item, "%H:%M")
        except ValueError as exc:
            raise ValueError(f"Invalid automatic refresh time {item!r}; use HH:MM") from exc
        normalized = moment.strftime("%H:%M")
        if normalized not in parsed:
            parsed.append(normalized)
    if not parsed:
        raise ValueError("At least one automatic refresh time is required")
    return tuple(sorted(parsed))


def update_interval_seconds() -> int:
    raw = os.getenv("TOKEN_POOL_UPDATE_INTERVAL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_UPDATE_INTERVAL_SECONDS
    return max(60, int(raw))


def latest_due_slot(
    now: datetime,
    refresh_times: tuple[str, ...],
    *,
    max_catchup: timedelta = MAX_SCHEDULE_CATCHUP,
) -> datetime | None:
    candidates: list[datetime] = []
    for day in (now.date(), (now - timedelta(days=1)).date()):
        for value in refresh_times:
            parsed = datetime.strptime(value, "%H:%M").time()
            candidate = datetime.combine(day, parsed, tzinfo=now.tzinfo)
            if candidate <= now:
                candidates.append(candidate)
    if not candidates:
        return None
    latest = max(candidates)
    return latest if now - latest <= max_catchup else None


def slot_key(value: datetime) -> str:
    return value.isoformat(timespec="minutes")


@dataclass
class AutomationState:
    path: Path
    last_work_refresh_slot: str = ""
    last_work_refresh_result: str = ""
    pending_work_refresh_slot: str = ""
    next_work_retry_at: str = ""
    work_retry_count: int = 0
    last_update_check_at: str = ""

    @classmethod
    def load(cls, root: Path | None = None) -> "AutomationState":
        path = (root or application_dir()) / "automation-state.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        return cls(
            path=path,
            last_work_refresh_slot=str(payload.get("last_work_refresh_slot") or ""),
            last_work_refresh_result=str(payload.get("last_work_refresh_result") or ""),
            pending_work_refresh_slot=str(payload.get("pending_work_refresh_slot") or ""),
            next_work_retry_at=str(payload.get("next_work_retry_at") or ""),
            work_retry_count=_nonnegative_int(payload.get("work_retry_count")),
            last_update_check_at=str(payload.get("last_update_check_at") or ""),
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 2,
            "last_work_refresh_slot": self.last_work_refresh_slot,
            "last_work_refresh_result": self.last_work_refresh_result,
            "pending_work_refresh_slot": self.pending_work_refresh_slot,
            "next_work_retry_at": self.next_work_retry_at,
            "work_retry_count": self.work_retry_count,
            "last_update_check_at": self.last_update_check_at,
        }
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def register_startup(root: Path | None = None) -> bool:
    """Register a per-user launch without requiring administrator rights."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return False
    import winreg

    install_root = (root or application_dir()).resolve()
    launcher = install_root / "Launch-TokenPoolClient.ps1"
    if not launcher.exists():
        return False
    command = (
        'powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden '
        f'-File "{launcher}" -Background'
    )
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, STARTUP_VALUE_NAME, 0, winreg.REG_SZ, command)
    return True


def current_release_tag(root: Path | None = None) -> str:
    path = (root or application_dir()) / "current.json"
    try:
        return str(json.loads(path.read_text(encoding="utf-8-sig")).get("tag") or "")
    except (OSError, json.JSONDecodeError):
        return ""


def check_for_update(root: Path | None = None, *, timeout_seconds: int = 600) -> tuple[bool, str, str]:
    install_root = (root or application_dir()).resolve()
    launcher = install_root / "Launch-TokenPoolClient.ps1"
    if not launcher.exists():
        return False, "", ""
    before = current_release_tag(install_root)
    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-WindowStyle",
        "Hidden",
        "-File",
        str(launcher),
        "-InstallOnly",
    ]
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    completed = subprocess.run(
        command,
        check=False,
        timeout=timeout_seconds,
        startupinfo=startupinfo,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Update check failed with exit code {completed.returncode}")
    after = current_release_tag(install_root)
    return bool(before and after and before != after), before, after


def restart_after_exit(root: Path | None = None, *, process_id: int | None = None) -> None:
    install_root = (root or application_dir()).resolve()
    launcher = install_root / "Launch-TokenPoolClient.ps1"
    if not launcher.exists():
        raise RuntimeError("The installed launcher is missing")
    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-WindowStyle",
        "Hidden",
        "-File",
        str(launcher),
        "-Background",
        "-WaitForProcessId",
        str(process_id or os.getpid()),
    ]
    subprocess.Popen(
        command,
        cwd=str(install_root),
        close_fds=True,
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
    )
