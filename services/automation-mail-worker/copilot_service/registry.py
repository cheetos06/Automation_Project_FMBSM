from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 1
TURN_HISTORY_RETENTION_SECONDS = 30 * 24 * 60 * 60


def default_data_dir() -> Path:
    configured = os.getenv("COPILOT_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path(__file__).resolve().parents[1] / "data" / "copilot").resolve()


@dataclass(frozen=True)
class AccountRecord:
    account_id: str
    username: str
    tenant_id: str
    object_id: str
    session_path: Path
    uploaded_at: float
    access_expires_at: float
    refresh_expires_at: float | None
    enabled: bool
    cooldown_until: float
    cooldown_reason: str | None
    last_error: str | None
    last_used_at: float | None
    total_turns: int

    def as_public_dict(self, *, now: float | None = None) -> dict[str, Any]:
        current = time.time() if now is None else now
        data = {
            "account_id": self.account_id,
            "username": _mask_username(self.username),
            "uploaded_at": self.uploaded_at,
            "access_expires_at": self.access_expires_at,
            "refresh_expires_at": self.refresh_expires_at,
            "enabled": self.enabled,
            "cooldown_until": self.cooldown_until,
            "cooldown_reason": self.cooldown_reason,
            "last_used_at": self.last_used_at,
            "total_turns": self.total_turns,
        }
        data["access_valid"] = self.access_expires_at > current + 60
        data["cooling_down"] = self.cooldown_until > current
        refresh_potentially_valid = self.refresh_expires_at is None or self.refresh_expires_at > current
        data["runtime_available"] = bool(
            self.enabled
            and not data["cooling_down"]
            and (data["access_valid"] or refresh_potentially_valid)
        )
        data["cooldown_remaining_seconds"] = round(max(0.0, self.cooldown_until - current), 1)
        data["access_remaining_seconds"] = round(max(0.0, self.access_expires_at - current), 1)
        return data


def _mask_username(value: str) -> str:
    local, separator, domain = value.partition("@")
    if not separator:
        return (local[:1] + "***") if local else "unknown"
    visible = local[:2] if len(local) > 1 else local[:1]
    return f"{visible}***@{domain}"


class CopilotRegistry:
    """Process-safe account metadata, turn accounting, and cooldown state."""

    def __init__(self, data_dir: Path | str | None = None, db_path: Path | str | None = None) -> None:
        self.data_dir = Path(data_dir or default_data_dir()).expanduser().resolve()
        self.accounts_dir = self.data_dir / "accounts"
        self.db_path = Path(db_path).expanduser().resolve() if db_path else self.data_dir / "registry.sqlite3"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.accounts_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @classmethod
    def from_env(cls) -> "CopilotRegistry":
        data_dir = os.getenv("COPILOT_DATA_DIR") or None
        db_path = os.getenv("COPILOT_REGISTRY_DB") or None
        return cls(data_dir=data_dir, db_path=db_path)

    @contextmanager
    def _connect(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            if immediate:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS accounts (
                    account_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    session_path TEXT NOT NULL,
                    uploaded_at REAL NOT NULL,
                    access_expires_at REAL NOT NULL,
                    refresh_expires_at REAL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    cooldown_until REAL NOT NULL DEFAULT 0,
                    cooldown_reason TEXT,
                    last_error TEXT,
                    last_used_at REAL,
                    total_turns INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
                    dispatched_at REAL NOT NULL,
                    job_id TEXT,
                    operation TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_turns_account_time
                    ON turns(account_id, dispatched_at);
                CREATE TABLE IF NOT EXISTS uploads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
                    uploaded_at REAL NOT NULL,
                    source_version TEXT,
                    bundle_sha256 TEXT NOT NULL,
                    remote_address TEXT
                );
                CREATE TABLE IF NOT EXISTS client_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_id TEXT NOT NULL,
                    received_at REAL NOT NULL,
                    observed_at REAL NOT NULL,
                    event TEXT NOT NULL,
                    scheduled_slot TEXT,
                    app_version TEXT,
                    account_ids TEXT,
                    remote_address TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_client_events_client_time
                    ON client_events(client_id, received_at DESC);
                CREATE TABLE IF NOT EXISTS clients (
                    client_id TEXT PRIMARY KEY,
                    first_seen_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    app_version TEXT,
                    account_ids TEXT,
                    last_event TEXT,
                    scheduled_slot TEXT,
                    status_json TEXT,
                    remote_address TEXT
                );
                CREATE TABLE IF NOT EXISTS client_commands (
                    command_id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL REFERENCES clients(client_id) ON DELETE CASCADE,
                    command TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    dispatched_at REAL,
                    lease_until REAL,
                    finished_at REAL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    result_json TEXT,
                    requested_by TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_client_commands_poll
                    ON client_commands(client_id, status, created_at);
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def upsert_account(
        self,
        *,
        account_id: str,
        username: str,
        tenant_id: str,
        object_id: str,
        session_path: Path,
        uploaded_at: float,
        access_expires_at: float,
        refresh_expires_at: float | None,
        source_version: str,
        bundle_sha256: str,
        remote_address: str | None,
    ) -> AccountRecord:
        with self._connect(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO accounts(
                    account_id, username, tenant_id, object_id, session_path,
                    uploaded_at, access_expires_at, refresh_expires_at, enabled,
                    cooldown_until, cooldown_reason, last_error, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 1, 0, NULL, NULL, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    username=excluded.username,
                    tenant_id=excluded.tenant_id,
                    object_id=excluded.object_id,
                    session_path=excluded.session_path,
                    uploaded_at=excluded.uploaded_at,
                    access_expires_at=excluded.access_expires_at,
                    refresh_expires_at=excluded.refresh_expires_at,
                    enabled=1,
                    last_error=NULL,
                    updated_at=excluded.updated_at
                """,
                (
                    account_id,
                    username,
                    tenant_id,
                    object_id,
                    str(session_path.resolve()),
                    uploaded_at,
                    access_expires_at,
                    refresh_expires_at,
                    uploaded_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO uploads(account_id, uploaded_at, source_version, bundle_sha256, remote_address)
                VALUES(?, ?, ?, ?, ?)
                """,
                (account_id, uploaded_at, source_version, bundle_sha256, remote_address),
            )
        record = self.get_account(account_id)
        if record is None:
            raise RuntimeError(f"Account {account_id} disappeared after upsert")
        return record

    def get_account(self, account_id: str) -> AccountRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM accounts WHERE account_id=?",
                (account_id,),
            ).fetchone()
        return self._record(row) if row else None

    def list_accounts(self, *, enabled_only: bool = True) -> list[AccountRecord]:
        query = "SELECT * FROM accounts"
        if enabled_only:
            query += " WHERE enabled=1"
        query += " ORDER BY uploaded_at DESC, account_id"
        with self._connect() as connection:
            rows = connection.execute(query).fetchall()
        return [self._record(row) for row in rows]

    def record_client_event(
        self,
        *,
        client_id: str,
        observed_at: float,
        event: str,
        scheduled_slot: str | None,
        app_version: str | None,
        account_ids: list[str],
        remote_address: str | None,
        status: dict[str, Any] | None = None,
    ) -> int:
        """Persist a small signed client-presence event for operational diagnosis."""

        now = time.time()
        with self._connect(immediate=True) as connection:
            cursor = connection.execute(
                """
                INSERT INTO client_events(
                    client_id, received_at, observed_at, event, scheduled_slot,
                    app_version, account_ids, remote_address
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    client_id,
                    now,
                    observed_at,
                    event,
                    scheduled_slot,
                    app_version,
                    ",".join(account_ids),
                    remote_address,
                ),
            )
            self._upsert_client_presence(
                connection,
                client_id=client_id,
                observed_at=observed_at,
                event=event,
                scheduled_slot=scheduled_slot,
                app_version=app_version,
                account_ids=account_ids,
                remote_address=remote_address,
                status=status,
            )
            # Two scheduled events per day are expected. Ninety days keeps the
            # diagnostic table small while leaving enough history for audits.
            connection.execute("DELETE FROM client_events WHERE received_at < ?", (now - 90 * 24 * 60 * 60,))
            return int(cursor.lastrowid)

    def update_client_presence(
        self,
        *,
        client_id: str,
        observed_at: float,
        event: str,
        scheduled_slot: str | None,
        app_version: str | None,
        account_ids: list[str],
        remote_address: str | None,
        status: dict[str, Any] | None = None,
    ) -> None:
        with self._connect(immediate=True) as connection:
            self._upsert_client_presence(
                connection,
                client_id=client_id,
                observed_at=observed_at,
                event=event,
                scheduled_slot=scheduled_slot,
                app_version=app_version,
                account_ids=account_ids,
                remote_address=remote_address,
                status=status,
            )

    @staticmethod
    def _upsert_client_presence(
        connection: sqlite3.Connection,
        *,
        client_id: str,
        observed_at: float,
        event: str,
        scheduled_slot: str | None,
        app_version: str | None,
        account_ids: list[str],
        remote_address: str | None,
        status: dict[str, Any] | None,
    ) -> None:
        status_json = json.dumps(status or {}, ensure_ascii=True, separators=(",", ":"))
        connection.execute(
            """
            INSERT INTO clients(
                client_id, first_seen_at, last_seen_at, app_version, account_ids,
                last_event, scheduled_slot, status_json, remote_address
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(client_id) DO UPDATE SET
                last_seen_at=excluded.last_seen_at,
                app_version=excluded.app_version,
                account_ids=excluded.account_ids,
                last_event=excluded.last_event,
                scheduled_slot=COALESCE(excluded.scheduled_slot, clients.scheduled_slot),
                status_json=excluded.status_json,
                remote_address=excluded.remote_address
            """,
            (
                client_id,
                observed_at,
                observed_at,
                app_version,
                ",".join(account_ids),
                event,
                scheduled_slot,
                status_json,
                remote_address,
            ),
        )

    def list_clients(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM clients ORDER BY last_seen_at DESC, client_id"
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                status = json.loads(str(row["status_json"] or "{}"))
            except json.JSONDecodeError:
                status = {}
            result.append(
                {
                    "client_id": str(row["client_id"]),
                    "first_seen_at": float(row["first_seen_at"]),
                    "last_seen_at": float(row["last_seen_at"]),
                    "app_version": str(row["app_version"] or ""),
                    "account_ids": [
                        value for value in str(row["account_ids"] or "").split(",") if value
                    ],
                    "last_event": str(row["last_event"] or ""),
                    "scheduled_slot": str(row["scheduled_slot"]) if row["scheduled_slot"] else None,
                    "status": status if isinstance(status, dict) else {},
                    "remote_address": str(row["remote_address"] or ""),
                }
            )
        return result

    def forget_unmapped_client(
        self,
        client_id: str,
        *,
        offline_before: float,
    ) -> tuple[bool, str]:
        """Remove one explicitly selected stale installation and its telemetry.

        The checks and deletion share one write transaction so a fresh heartbeat
        or newly mapped account cannot race an administrator's deletion request.
        """

        with self._connect(immediate=True) as connection:
            row = connection.execute(
                "SELECT last_seen_at, account_ids FROM clients WHERE client_id=?",
                (client_id,),
            ).fetchone()
            if row is None:
                return False, "unknown_client"
            if float(row["last_seen_at"]) >= float(offline_before):
                return False, "client_online"
            if any(value for value in str(row["account_ids"] or "").split(",") if value):
                return False, "client_has_accounts"
            connection.execute("DELETE FROM client_events WHERE client_id=?", (client_id,))
            connection.execute("DELETE FROM clients WHERE client_id=?", (client_id,))
        return True, "forgotten"

    def create_client_command(
        self,
        *,
        client_id: str,
        command: str,
        payload: dict[str, Any],
        expires_in_seconds: float,
        requested_by: str = "admin",
    ) -> dict[str, Any]:
        now = time.time()
        command_id = uuid.uuid4().hex
        with self._connect(immediate=True) as connection:
            exists = connection.execute(
                "SELECT 1 FROM clients WHERE client_id=?", (client_id,)
            ).fetchone()
            if exists is None:
                raise KeyError(f"Unknown client: {client_id}")
            connection.execute(
                """
                INSERT INTO client_commands(
                    command_id, client_id, command, payload_json, status,
                    created_at, expires_at, requested_by
                ) VALUES(?, ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    command_id,
                    client_id,
                    command,
                    json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
                    now,
                    now + max(60.0, min(float(expires_in_seconds), 24 * 60 * 60)),
                    requested_by,
                ),
            )
        return self.get_client_command(command_id) or {}

    def poll_client_command(self, client_id: str, *, lease_seconds: float = 180) -> dict[str, Any] | None:
        now = time.time()
        with self._connect(immediate=True) as connection:
            connection.execute(
                """
                UPDATE client_commands
                SET status='expired', finished_at=?
                WHERE client_id=? AND status IN ('queued', 'dispatched') AND expires_at<=?
                """,
                (now, client_id, now),
            )
            connection.execute(
                """
                UPDATE client_commands
                SET status=CASE WHEN attempts>=3 THEN 'failed' ELSE 'queued' END,
                    finished_at=CASE WHEN attempts>=3 THEN ? ELSE NULL END,
                    result_json=CASE WHEN attempts>=3 THEN ? ELSE result_json END,
                    lease_until=NULL
                WHERE client_id=? AND status='dispatched' AND lease_until<=?
                """,
                (
                    now,
                    json.dumps({"error": "client command lease expired three times"}),
                    client_id,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM client_commands
                WHERE client_id=? AND status='queued' AND expires_at>?
                ORDER BY created_at, command_id
                LIMIT 1
                """,
                (client_id, now),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE client_commands
                SET status='dispatched', dispatched_at=?, lease_until=?, attempts=attempts+1
                WHERE command_id=?
                """,
                (now, now + max(60.0, lease_seconds), str(row["command_id"])),
            )
        return self.get_client_command(str(row["command_id"]))

    def complete_client_command(
        self,
        *,
        client_id: str,
        command_id: str,
        succeeded: bool,
        result: dict[str, Any],
    ) -> bool:
        now = time.time()
        with self._connect(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE client_commands
                SET status=?, finished_at=?, lease_until=NULL, result_json=?
                WHERE command_id=? AND client_id=? AND status IN ('queued', 'dispatched')
                """,
                (
                    "completed" if succeeded else "failed",
                    now,
                    json.dumps(result, ensure_ascii=True, separators=(",", ":"))[:8000],
                    command_id,
                    client_id,
                ),
            )
            return cursor.rowcount == 1

    def cancel_client_command(self, command_id: str) -> bool:
        now = time.time()
        with self._connect(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE client_commands SET status='cancelled', finished_at=?, lease_until=NULL
                WHERE command_id=? AND status IN ('queued', 'dispatched')
                """,
                (now, command_id),
            )
            return cursor.rowcount == 1

    def get_client_command(self, command_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM client_commands WHERE command_id=?", (command_id,)
            ).fetchone()
        return self._client_command(row) if row else None

    def list_client_commands(self, *, limit: int = 100) -> list[dict[str, Any]]:
        bounded_limit = min(max(1, int(limit)), 500)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM client_commands ORDER BY created_at DESC LIMIT ?",
                (bounded_limit,),
            ).fetchall()
        return [self._client_command(row) for row in rows]

    @staticmethod
    def _client_command(row: sqlite3.Row) -> dict[str, Any]:
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except json.JSONDecodeError:
            payload = {}
        try:
            result = json.loads(str(row["result_json"] or "{}"))
        except json.JSONDecodeError:
            result = {}
        return {
            "command_id": str(row["command_id"]),
            "client_id": str(row["client_id"]),
            "command": str(row["command"]),
            "payload": payload if isinstance(payload, dict) else {},
            "status": str(row["status"]),
            "created_at": float(row["created_at"]),
            "expires_at": float(row["expires_at"]),
            "dispatched_at": float(row["dispatched_at"]) if row["dispatched_at"] else None,
            "finished_at": float(row["finished_at"]) if row["finished_at"] else None,
            "attempts": int(row["attempts"] or 0),
            "result": result if isinstance(result, dict) else {},
            "requested_by": str(row["requested_by"] or ""),
        }

    def recent_client_events(self, *, limit: int = 50) -> list[dict[str, Any]]:
        bounded_limit = min(max(1, int(limit)), 200)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, client_id, received_at, observed_at, event,
                       scheduled_slot, app_version, account_ids, remote_address
                FROM client_events
                ORDER BY received_at DESC, id DESC
                LIMIT ?
                """,
                (bounded_limit,),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "client_id": str(row["client_id"]),
                "received_at": float(row["received_at"]),
                "observed_at": float(row["observed_at"]),
                "event": str(row["event"]),
                "scheduled_slot": str(row["scheduled_slot"]) if row["scheduled_slot"] else None,
                "app_version": str(row["app_version"]) if row["app_version"] else None,
                "account_ids": [
                    value for value in str(row["account_ids"] or "").split(",") if value
                ],
                "remote_address": str(row["remote_address"]) if row["remote_address"] else None,
            }
            for row in rows
        ]

    def update_token_state(
        self,
        account_id: str,
        *,
        session_path: Path | None = None,
        access_expires_at: float,
        refresh_expires_at: float | None = None,
        error: str | None = None,
    ) -> None:
        with self._connect(immediate=True) as connection:
            if session_path is None:
                connection.execute(
                    """
                    UPDATE accounts
                    SET access_expires_at=?, refresh_expires_at=COALESCE(?, refresh_expires_at),
                        last_error=?, updated_at=?
                    WHERE account_id=?
                    """,
                    (access_expires_at, refresh_expires_at, error, time.time(), account_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE accounts
                    SET session_path=?, access_expires_at=?,
                        refresh_expires_at=COALESCE(?, refresh_expires_at),
                        last_error=?, updated_at=?
                    WHERE account_id=?
                    """,
                    (
                        str(session_path.resolve()),
                        access_expires_at,
                        refresh_expires_at,
                        error,
                        time.time(),
                        account_id,
                    ),
                )

    def mark_error(self, account_id: str, message: str, *, disable: bool = False) -> None:
        with self._connect(immediate=True) as connection:
            connection.execute(
                "UPDATE accounts SET last_error=?, enabled=CASE WHEN ? THEN 0 ELSE enabled END, updated_at=? WHERE account_id=?",
                (message[:1000], int(disable), time.time(), account_id),
            )

    def reserve_turn(
        self,
        account_id: str,
        *,
        turn_limit: int,
        window_seconds: float,
        cooldown_seconds: float,
        job_id: str | None = None,
        operation: str | None = None,
    ) -> tuple[bool, float, str | None, int]:
        """Atomically reserve one turn and return availability plus current count."""

        now = time.time()
        with self._connect(immediate=True) as connection:
            row = connection.execute(
                "SELECT enabled, cooldown_until FROM accounts WHERE account_id=?",
                (account_id,),
            ).fetchone()
            if row is None:
                return False, 0.0, "missing", 0
            if not bool(row["enabled"]):
                return False, 0.0, "disabled", 0

            cooldown_until = float(row["cooldown_until"] or 0.0)
            if cooldown_until > now:
                count = self._turn_count(connection, account_id, now - window_seconds)
                return False, cooldown_until - now, "cooldown", count
            if cooldown_until:
                connection.execute(
                    "UPDATE accounts SET cooldown_until=0, cooldown_reason=NULL, updated_at=? WHERE account_id=?",
                    (now, account_id),
                )

            # Cooldown decisions use ``window_seconds`` (normally one hour),
            # but dashboard reporting also needs a truthful 24-hour view.
            # Keep a bounded 30-day audit window instead of deleting everything
            # older than the active cooldown window on every reservation.
            connection.execute(
                "DELETE FROM turns WHERE dispatched_at<?",
                (now - TURN_HISTORY_RETENTION_SECONDS,),
            )
            count = self._turn_count(connection, account_id, now - window_seconds)
            if count >= turn_limit:
                until = now + cooldown_seconds
                connection.execute(
                    "UPDATE accounts SET cooldown_until=?, cooldown_reason='turn_limit', updated_at=? WHERE account_id=?",
                    (until, now, account_id),
                )
                return False, cooldown_seconds, "turn_limit", count

            connection.execute(
                "INSERT INTO turns(account_id, dispatched_at, job_id, operation) VALUES(?, ?, ?, ?)",
                (account_id, now, job_id, operation),
            )
            new_count = count + 1
            cooldown_until_after = now + cooldown_seconds if new_count >= turn_limit else 0.0
            cooldown_reason = "turn_limit" if cooldown_until_after else None
            connection.execute(
                """
                UPDATE accounts
                SET last_used_at=?, total_turns=total_turns+1,
                    cooldown_until=?, cooldown_reason=?, updated_at=?
                WHERE account_id=?
                """,
                (now, cooldown_until_after, cooldown_reason, now, account_id),
            )
            return True, 0.0, cooldown_reason, new_count

    def start_cooldown(self, account_id: str, *, seconds: float, reason: str, error: str | None = None) -> None:
        now = time.time()
        with self._connect(immediate=True) as connection:
            row = connection.execute(
                "SELECT cooldown_until FROM accounts WHERE account_id=?",
                (account_id,),
            ).fetchone()
            if row is None:
                return
            until = max(float(row["cooldown_until"] or 0.0), now + seconds)
            connection.execute(
                """
                UPDATE accounts SET cooldown_until=?, cooldown_reason=?, last_error=?, updated_at=?
                WHERE account_id=?
                """,
                (until, reason, error[:1000] if error else None, now, account_id),
            )

    def status(self) -> dict[str, Any]:
        now = time.time()
        accounts = self.list_accounts(enabled_only=False)
        usage = self.turn_usage(now=now)
        with self._connect() as connection:
            upload_count = connection.execute("SELECT COUNT(*) FROM uploads").fetchone()[0]
        account_values: list[dict[str, Any]] = []
        for account in accounts:
            value = account.as_public_dict(now=now)
            value.update(usage.get(account.account_id, _empty_turn_usage()))
            account_values.append(value)
        return {
            "schema_version": SCHEMA_VERSION,
            "now": now,
            "account_count": len(accounts),
            "enabled_account_count": sum(account.enabled for account in accounts),
            "available_account_count": sum(
                account.enabled
                and account.cooldown_until <= now
                and (account.access_expires_at > now + 60 or account.refresh_expires_at is None or account.refresh_expires_at > now)
                for account in accounts
            ),
            # ``recent_turns`` remains for older clients and means last hour.
            "recent_turns": sum(item["turns_last_hour"] for item in usage.values()),
            "turns_last_hour": sum(item["turns_last_hour"] for item in usage.values()),
            "turns_last_24_hours": sum(
                item["turns_last_24_hours"] for item in usage.values()
            ),
            "upload_count": int(upload_count),
            "accounts": account_values,
        }

    def turn_usage(self, *, now: float | None = None) -> dict[str, dict[str, int]]:
        current = time.time() if now is None else float(now)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT account_id,
                       SUM(CASE WHEN dispatched_at>=? THEN 1 ELSE 0 END) AS last_hour,
                       SUM(CASE WHEN dispatched_at>=? THEN 1 ELSE 0 END) AS last_day
                FROM turns
                WHERE dispatched_at>=?
                GROUP BY account_id
                """,
                (current - 3600, current - 24 * 60 * 60, current - 24 * 60 * 60),
            ).fetchall()
        return {
            str(row["account_id"]): {
                "turns_last_hour": int(row["last_hour"] or 0),
                "turns_last_24_hours": int(row["last_day"] or 0),
            }
            for row in rows
        }

    @staticmethod
    def _turn_count(connection: sqlite3.Connection, account_id: str, cutoff: float) -> int:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM turns WHERE account_id=? AND dispatched_at>=?",
                (account_id, cutoff),
            ).fetchone()[0]
        )

    @staticmethod
    def _record(row: sqlite3.Row) -> AccountRecord:
        return AccountRecord(
            account_id=str(row["account_id"]),
            username=str(row["username"]),
            tenant_id=str(row["tenant_id"]),
            object_id=str(row["object_id"]),
            session_path=Path(str(row["session_path"])),
            uploaded_at=float(row["uploaded_at"]),
            access_expires_at=float(row["access_expires_at"]),
            refresh_expires_at=float(row["refresh_expires_at"]) if row["refresh_expires_at"] is not None else None,
            enabled=bool(row["enabled"]),
            cooldown_until=float(row["cooldown_until"] or 0.0),
            cooldown_reason=str(row["cooldown_reason"]) if row["cooldown_reason"] else None,
            last_error=str(row["last_error"]) if row["last_error"] else None,
            last_used_at=float(row["last_used_at"]) if row["last_used_at"] is not None else None,
            total_turns=int(row["total_turns"] or 0),
        )


def _empty_turn_usage() -> dict[str, int]:
    return {"turns_last_hour": 0, "turns_last_24_hours": 0}
