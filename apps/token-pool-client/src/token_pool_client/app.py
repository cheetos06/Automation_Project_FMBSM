from __future__ import annotations

import base64
import os
import shutil
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, X, messagebox
import tkinter as tk
from tkinter import ttk
from typing import Callable

from . import __version__
from . import bootstrap
from .automation import (
    AutomationState,
    SingleInstance,
    check_for_update,
    configured_refresh_times,
    is_automatic_work_account,
    latest_due_slot,
    register_startup,
    restart_after_exit,
    slot_key,
    update_interval_seconds,
)
from .bundle import create_bundle
from .control import (
    flush_admin_command_results,
    poll_admin_commands,
    queue_admin_command_result,
    send_client_heartbeat,
)
from .refresh import initialize_refresh, refresh_existing
from .storage import AccountStore, ClientAccount
from .tray import NotificationTray
from .upload import (
    ClientConfig,
    ServerRejectedError,
    client_preflight,
    is_transient_network_error,
    load_config,
    server_status,
    upload_bundle,
)


SAMPLE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAV0lEQVR4nO3PQQ0AIBDAsAP/nuGNAvZoFSzZOjNnyNi1dwfgUQFeFeBVAV4V4FUBXhXgVQFeFeBVAV4V4FUBXhXgVQFeFeBVAV4V4FUBXhXgVQFeFeBVAV4V4FUB3gA5ggJ/QlTzGAAAAABJRU5ErkJggg=="
)
MAX_SCHEDULED_NETWORK_RETRIES = 2


@dataclass(frozen=True)
class RenewalBatchResult:
    successes: int = 0
    permanent_failures: int = 0
    transient_failures: int = 0

    @property
    def failures(self) -> int:
        return self.permanent_failures + self.transient_failures

    @property
    def waiting_for_network(self) -> bool:
        return self.transient_failures > 0


class TokenPoolApp:
    def __init__(self, root: tk.Tk, *, background: bool, instance: SingleInstance) -> None:
        self.root = root
        self.background = background
        self.instance = instance
        self.store = AccountStore()
        self.config: ClientConfig | None = None
        self.busy = False
        self.current_activity = "Ready"
        self.shutting_down = False
        self.refresh_times = configured_refresh_times()
        self.update_interval = update_interval_seconds()
        self.automation_state = AutomationState.load(self.store.root)
        self.next_update_check = time.monotonic() + self.update_interval
        self.next_control_poll = time.monotonic() + 5
        self.control_polling = False
        self.next_presence_heartbeat = 0.0
        self.presence_heartbeat_running = False
        self.log_path = self.store.root / "client.log"
        self.log_lock = threading.Lock()
        self.root.title(f"FMBSM Token Pool Client {__version__}")
        self.root.geometry("980x640")
        self.root.minsize(820, 540)
        self.root.configure(bg="#f3f6f8")
        self.logo = self._load_logo()
        if self.logo is not None:
            self.root.iconphoto(True, self.logo)
        self._style()
        self._build()
        self._load_config()
        self._refresh_table()
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self.tray = NotificationTray(
            self._logo_path("token-pool-logo.ico"),
            on_show=lambda: self.root.after(0, self.show_window),
            on_refresh=lambda: self.root.after(0, self.refresh_work_now),
            on_update=lambda: self.root.after(0, lambda: self.check_updates(manual=True)),
            on_exit=lambda: self.root.after(0, self.shutdown),
        )
        if not self.tray.start():
            self.log("Windows notification-area icon could not be created.")
        if self.background:
            self.root.withdraw()
        self.root.after(750, self._start_automation)

    def _style(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f3f6f8")
        style.configure("Card.TFrame", background="white", relief="flat")
        style.configure("Title.TLabel", background="#123b4a", foreground="white", font=("Segoe UI Semibold", 20))
        style.configure("Subtitle.TLabel", background="#123b4a", foreground="#cfe3ea", font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI Semibold", 10), padding=(14, 9))
        style.configure("Primary.TButton", background="#0f7c8c", foreground="white")
        style.map("Primary.TButton", background=[("active", "#0b6572"), ("disabled", "#9fb8bd")])
        style.configure("Treeview", rowheight=29, font=("Segoe UI", 10), background="white", fieldbackground="white")
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 10))

    def _logo_path(self, name: str) -> Path:
        candidates: list[Path] = []
        if getattr(sys, "frozen", False):
            candidates.append(Path(sys.executable).resolve().parent / name)
        candidates.append(Path(__file__).resolve().parents[2] / "assets" / name)
        return next((path for path in candidates if path.exists()), candidates[-1])

    def _load_logo(self) -> tk.PhotoImage | None:
        try:
            return tk.PhotoImage(file=str(self._logo_path("token-pool-logo.png")))
        except Exception:
            return None

    def _build(self) -> None:
        header = tk.Frame(self.root, bg="#123b4a", height=94)
        header.pack(fill=X)
        header.pack_propagate(False)
        if self.logo is not None:
            tk.Label(header, image=self.logo, bg="#123b4a", borderwidth=0).pack(side=LEFT, padx=(22, 12), pady=14)
        heading = tk.Frame(header, bg="#123b4a")
        heading.pack(side=LEFT, fill=BOTH, expand=True)
        ttk.Label(heading, text="FMBSM Copilot Token Pool", style="Title.TLabel").pack(anchor="w", pady=(17, 0))
        ttk.Label(
            heading,
            text="Fresh Microsoft authorization for work accounts, with secure upload to the AWS automation pool.",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        controls = ttk.Frame(self.root)
        controls.pack(fill=X, padx=22, pady=(18, 10))
        self.refresh_button = ttk.Button(
            controls,
            text="Renew & upload all now",
            style="Primary.TButton",
            command=self.refresh_all,
        )
        self.refresh_button.pack(side=LEFT)
        self.add_button = ttk.Button(controls, text="Add Microsoft account", command=self.add_account)
        self.add_button.pack(side=LEFT, padx=8)
        self.status_button = ttk.Button(controls, text="Check my AWS status", command=self.show_status)
        self.status_button.pack(side=LEFT)
        self.state_label = ttk.Label(controls, text="Ready", font=("Segoe UI Semibold", 10))
        self.state_label.pack(side=RIGHT, padx=5)

        card = ttk.Frame(self.root, style="Card.TFrame")
        card.pack(fill=BOTH, expand=True, padx=22, pady=(0, 12))
        columns = ("account", "automation", "expires", "uploaded", "status")
        self.table = ttk.Treeview(card, columns=columns, show="headings", height=8)
        for column, label, width in (
            ("account", "Microsoft account", 265),
            ("automation", "Automatic renewal", 140),
            ("expires", "Microsoft sign-in valid until", 190),
            ("uploaded", "Last uploaded", 155),
            ("status", "Status", 210),
        ):
            self.table.heading(column, text=label)
            self.table.column(column, width=width, minwidth=110, anchor="w")
        self.table.pack(fill=BOTH, expand=True, padx=12, pady=12)

        log_frame = ttk.Frame(self.root)
        log_frame.pack(fill=BOTH, padx=22, pady=(0, 18))
        ttk.Label(log_frame, text="Activity", font=("Segoe UI Semibold", 10)).pack(anchor="w")
        self.log_box = tk.Text(
            log_frame,
            height=8,
            bg="#17252b",
            fg="#d8edf2",
            insertbackground="white",
            relief="flat",
            font=("Cascadia Mono", 9),
            padx=10,
            pady=8,
            state="disabled",
        )
        self.log_box.pack(fill=BOTH, expand=False, pady=(5, 0))

    def _load_config(self) -> None:
        try:
            self.config = load_config()
            self.log(f"Connected configuration: {self.config.endpoint}")
        except Exception as exc:
            self.log(f"Configuration error: {exc}")
            if not self.background:
                messagebox.showerror("Configuration missing", str(exc))

    def _refresh_table(self) -> None:
        for item in self.table.get_children():
            self.table.delete(item)
        now = time.time()
        schedule = " / ".join(self.refresh_times)
        for account in self.store.load():
            authorization_expires_at = _authorization_expires_at(account)
            expires = _time_text(authorization_expires_at)
            uploaded = _time_text(account.last_uploaded_at) if account.last_uploaded_at else "Never"
            status = _account_status(account, now=now)
            automation = schedule if is_automatic_work_account(account.username) else "Manual only"
            self.table.insert(
                "",
                END,
                iid=account.account_id,
                values=(account.username, automation, expires, uploaded, status),
            )

    def log(self, message: str) -> None:
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z")
        line = f"[{timestamp}] {message}"
        try:
            with self.log_lock:
                with self.log_path.open("a", encoding="utf-8") as output:
                    output.write(line + "\n")
        except OSError:
            pass

        def append() -> None:
            if self.shutting_down:
                return
            self.log_box.configure(state="normal")
            self.log_box.insert(END, f"[{datetime.now().strftime('%H:%M:%S')}] {message}\n")
            self.log_box.see(END)
            self.log_box.configure(state="disabled")

        try:
            self.root.after(0, append)
        except RuntimeError:
            pass

    def _set_busy(self, busy: bool, text: str = "Ready") -> None:
        self.busy = busy
        self.current_activity = text if busy else "Ready"
        state = "disabled" if busy else "normal"
        for button in (self.refresh_button, self.add_button, self.status_button):
            button.configure(state=state)
        self.state_label.configure(text=text)
        if busy:
            self.next_presence_heartbeat = 0.0
            self._send_busy_heartbeat()
        else:
            self.next_control_poll = 0.0

    def _background(
        self,
        label: str,
        work: Callable[[], object],
        *,
        on_complete: Callable[[object | None, BaseException | None], None] | None = None,
        show_error: bool = True,
    ) -> bool:
        if self.busy or self.shutting_down:
            return False
        self._set_busy(True, label)

        def runner() -> None:
            result: object | None = None
            failure: BaseException | None = None
            try:
                result = work()
            except Exception as exc:
                failure = exc
                self.log(f"ERROR: {exc}")
                if show_error:
                    self.root.after(0, lambda: messagebox.showerror("Token Pool Client", str(exc)))
            finally:
                if on_complete is not None:
                    try:
                        on_complete(result, failure)
                    except Exception as exc:
                        self.log(f"Completion handler failed: {exc}")
                self.root.after(0, self._refresh_table)
                self.root.after(0, lambda: self._set_busy(False))

        threading.Thread(target=runner, name="token-pool-work", daemon=True).start()
        return True

    def _browser_interaction(self, message: str) -> None:
        event = threading.Event()

        def show() -> None:
            self.show_window()
            messagebox.showinfo("Microsoft sign-in required", message)
            event.set()

        self.root.after(0, show)
        event.wait()

    def _fresh_renewal(
        self,
        account: ClientAccount,
        *,
        automatic: bool,
        allow_visible: bool = True,
    ) -> ClientAccount:
        self.log(f"Requesting a new 24-hour Microsoft authorization for {account.username}...")
        try:
            summary = bootstrap.renew_microsoft_session(
                site="https://m365.cloud.microsoft/chat",
                profile_dir=account.profile_path,
                session_dir=account.session_path,
                expected_username=account.username,
                expected_tenant=account.tenant_id,
                channel="msedge",
                timeout_seconds=int(os.getenv("TOKEN_POOL_HEADLESS_AUTH_TIMEOUT", "30")),
                progress=self.log,
                mode="silent",
                prompt_user=False,
            )
            method = "headless Microsoft SSO"
        except bootstrap.InteractiveAuthenticationRequired as exc:
            self.log(f"Silent fresh authorization needs user interaction: {exc}")
            if not allow_visible:
                self.log(
                    f"Silent-only renewal stopped for {account.username}; Edge was not opened."
                )
                raise
            self.log(f"Opening Edge for {account.username}; complete sign-in/MFA if Microsoft asks...")
            manual_multi_account = not is_automatic_work_account(account.username)
            if not automatic:
                bootstrap.set_interaction_callback(self._browser_interaction)
            try:
                summary = bootstrap.renew_microsoft_session(
                    site="https://m365.cloud.microsoft/chat",
                    profile_dir=account.profile_path,
                    session_dir=account.session_path,
                    expected_username=account.username,
                    expected_tenant=account.tenant_id,
                    channel="msedge",
                    timeout_seconds=int(os.getenv("TOKEN_POOL_VISIBLE_AUTH_TIMEOUT", "300")),
                    progress=self.log,
                    mode="select_account" if manual_multi_account else "expected_account",
                    prompt_user=not automatic,
                )
            finally:
                if not automatic:
                    bootstrap.set_interaction_callback(None)
            method = "visible Microsoft sign-in"
        self.log(
            f"Fresh authorization captured for {account.username} via {method} "
            f"(MSAL updated={summary.get('refresh_updated_at')})."
        )
        renewed, _ = initialize_refresh(
            account.session_path,
            account.profile_path,
            expected_account=account,
        )
        renewed.last_uploaded_at = account.last_uploaded_at
        return renewed

    def _renew_and_upload(
        self,
        accounts: list[ClientAccount],
        *,
        automatic: bool,
        client_event: str = "refresh_preflight",
        scheduled_slot: str | None = None,
        allow_visible: bool = True,
    ) -> RenewalBatchResult:
        if not self.config:
            raise RuntimeError("Client configuration is unavailable")
        try:
            client_preflight(
                self.config,
                event=client_event,
                account_ids=[account.account_id for account in accounts],
                scheduled_slot=scheduled_slot,
                status=self._client_control_status(),
            )
        except Exception as exc:
            if _is_transient_failure(exc):
                self.log(f"Network/AWS preflight unavailable; renewal remains pending: {exc}")
                return RenewalBatchResult(transient_failures=len(accounts))
            raise
        successes = 0
        permanent_failures = 0
        transient_failures = 0
        for account in accounts:
            try:
                if account.pending_upload and (_authorization_expires_at(account) or 0) > time.time() + 300:
                    renewed = account
                    if renewed.access_expires_at <= time.time() + 120:
                        renewed, _ = refresh_existing(renewed)
                    self.log(f"Retrying the pending AWS upload for {account.username} without another sign-in...")
                else:
                    renewed = self._fresh_renewal(
                        account,
                        automatic=automatic,
                        allow_visible=allow_visible,
                    )
                renewed.pending_upload = True
                renewed.last_error = None
                self.store.upsert(renewed)
            except Exception as exc:
                if _is_transient_failure(exc):
                    self.log(f"Network unavailable while renewing {account.username}; it will retry automatically: {exc}")
                    transient_failures += len(accounts) - successes - permanent_failures
                    break
                account.pending_upload = False
                account.last_error = str(exc)[:300]
                self.store.upsert(account)
                self.log(f"Microsoft authorization failed for {account.username}: {exc}")
                permanent_failures += 1
                continue

            try:
                upload_bundle(self.config, create_bundle(renewed))
                renewed.last_uploaded_at = time.time()
                renewed.last_error = None
                renewed.pending_upload = False
                self.store.upsert(renewed)
                self.log(f"Uploaded {renewed.username}; AWS accepted the fresh authorization.")
                successes += 1
            except Exception as exc:
                if _is_transient_failure(exc):
                    self.log(f"AWS upload for {account.username} is pending and will retry automatically: {exc}")
                    transient_failures += len(accounts) - successes - permanent_failures
                    break
                renewed.pending_upload = False
                renewed.last_error = str(exc)[:300]
                self.store.upsert(renewed)
                self.log(f"AWS rejected the upload for {account.username}: {exc}")
                permanent_failures += 1
        return RenewalBatchResult(
            successes=successes,
            permanent_failures=permanent_failures,
            transient_failures=transient_failures,
        )

    def refresh_all(self) -> None:
        accounts = self.store.load()
        if not accounts:
            self.log("No account is configured. Choose Add Microsoft account first.")
            return

        def completed(result: object | None, failure: BaseException | None) -> None:
            batch = result if isinstance(result, RenewalBatchResult) else RenewalBatchResult(permanent_failures=len(accounts))
            self._notify_renewal_result(batch, failure=failure)

        self._background(
            "Renewing all accounts...",
            lambda: self._renew_and_upload(
                accounts,
                automatic=False,
                client_event="manual_all_refresh",
            ),
            on_complete=completed,
        )

    def refresh_work_now(self) -> None:
        accounts = [account for account in self.store.load() if is_automatic_work_account(account.username)]
        if not accounts:
            self.log("No @forvismazars.com or @mazars.fr work account is configured for automatic renewal.")
            return

        def completed(result: object | None, failure: BaseException | None) -> None:
            batch = result if isinstance(result, RenewalBatchResult) else RenewalBatchResult(permanent_failures=len(accounts))
            self._notify_renewal_result(batch, failure=failure)

        self._background(
            "Renewing work account...",
            lambda: self._renew_and_upload(
                accounts,
                automatic=True,
                client_event="manual_work_refresh",
            ),
            on_complete=completed,
            show_error=False,
        )

    def _notify_renewal_result(
        self,
        result: RenewalBatchResult,
        *,
        failure: BaseException | None = None,
    ) -> None:
        if result.waiting_for_network or (failure is not None and _is_transient_failure(failure)):
            if result.permanent_failures:
                self.tray.notify(
                    "FMBSM renewal needs attention",
                    "One account needs sign-in; network-delayed accounts will retry automatically.",
                )
            else:
                self.tray.notify(
                    "FMBSM renewal waiting for network",
                    "The internet or AWS service was unavailable. The app will retry automatically.",
                )
        elif failure is not None or result.permanent_failures:
            self.tray.notify(
                "Microsoft sign-in required",
                "Your work-account token was not renewed. Open Token Pool Client and complete Microsoft sign-in.",
            )
        elif result.successes:
            self.tray.notify("FMBSM token renewed", "Your work-account session was refreshed and uploaded successfully.")

    def add_account(self) -> None:
        if not self.config:
            return

        def work() -> None:
            onboarding = self.store.accounts_root / f"onboarding-{uuid.uuid4().hex[:10]}"
            session = onboarding / "session"
            profile = onboarding / "edge-profile"
            session.mkdir(parents=True)
            profile.mkdir(parents=True)
            sample = onboarding / "bootstrap.png"
            sample.write_bytes(SAMPLE_PNG)

            bootstrap.set_interaction_callback(self._browser_interaction)
            try:
                self.log("Opening a dedicated Edge window for Microsoft sign-in and Copilot bootstrap...")
                bootstrap.bootstrap_api_session(
                    site="https://m365.cloud.microsoft/chat",
                    profile_dir=profile,
                    session_dir=session,
                    input_path=sample,
                    page_number=1,
                    prompt='Reply with only {"bootstrap": true}',
                    dpi=96,
                    channel="msedge",
                    timeout_seconds=90,
                    headless=False,
                )
                account, _ = initialize_refresh(session, profile)
                try:
                    upload_bundle(self.config, create_bundle(account))
                except ServerRejectedError as exc:
                    if exc.detail.get("action") != "interactive_mfa_required":
                        raise
                    code = str(exc.detail.get("microsoft_error_code") or "Microsoft policy")
                    self.log(
                        f"{code}: AWS requires fresh Microsoft MFA for {account.username}; "
                        "reopening Edge for one interactive authorization..."
                    )
                    bootstrap.renew_microsoft_session(
                        site="https://m365.cloud.microsoft/chat",
                        profile_dir=profile,
                        session_dir=session,
                        expected_username=account.username,
                        expected_tenant=account.tenant_id,
                        channel="msedge",
                        timeout_seconds=int(os.getenv("TOKEN_POOL_VISIBLE_AUTH_TIMEOUT", "300")),
                        progress=self.log,
                        mode="expected_account",
                        prompt_user=True,
                        force_mfa=True,
                    )
                    account, _ = initialize_refresh(
                        session,
                        profile,
                        expected_account=account,
                    )
                    upload_bundle(self.config, create_bundle(account))
                final_session, final_profile = self.store.account_paths(account.account_id)
                shutil.copytree(session, final_session, dirs_exist_ok=True)
                shutil.copytree(profile, final_profile, dirs_exist_ok=True)
                account.session_dir = str(final_session)
                account.profile_dir = str(final_profile)
                account.last_uploaded_at = time.time()
                self.store.upsert(account)
                self.log(
                    f"Added and uploaded {account.username}; AWS accepted the fresh authorization."
                )
            finally:
                bootstrap.set_interaction_callback(None)
                shutil.rmtree(onboarding, ignore_errors=True)

        self._background("Adding account...", work)

    def show_status(self) -> None:
        if not self.config:
            return
        local_accounts = self.store.load()

        def work() -> None:
            response = server_status(
                self.config,
                account_ids=[account.account_id for account in local_accounts],
                status=self._client_control_status(),
            )
            text = _my_aws_status_text(local_accounts, response)
            self.log(text.replace("\n", " | "))
            self.root.after(0, lambda: messagebox.showinfo("Your AWS token status", text))

        self._background("Checking server...", work)

    def _start_automation(self) -> None:
        try:
            registered = register_startup(self.store.root)
            self.log("Per-user Windows startup launch is registered." if registered else "Startup registration is active only in the installed app.")
        except Exception as exc:
            self.log(f"Could not register Windows startup launch: {exc}")
        self.log(
            f"Automatic work-account authorization: {', '.join(self.refresh_times)} local time. "
            f"ISGA and other accounts: manual only. Update check: every {self.update_interval // 60} minute(s)."
        )
        self.root.after(1500, self._scheduler_tick)

    def _client_control_status(self) -> dict[str, object]:
        accounts = self.store.load()
        state = getattr(self, "automation_state", None)
        return {
            "busy": bool(getattr(self, "busy", False)),
            "activity": str(getattr(self, "current_activity", "Ready") or "Ready")[:120],
            "last_work_refresh_result": str(
                getattr(state, "last_work_refresh_result", "") or ""
            ),
            "pending_work_refresh_slot": str(
                getattr(state, "pending_work_refresh_slot", "") or ""
            ),
            "accounts": [
                {
                    "account_id": account.account_id,
                    "authorization_expires_at": _authorization_expires_at(account),
                    "access_expires_at": account.access_expires_at,
                    "last_uploaded_at": account.last_uploaded_at,
                    "pending_upload": account.pending_upload,
                    "status": _account_status(account),
                }
                for account in accounts
            ],
        }

    def _send_busy_heartbeat(self) -> None:
        """Keep long Microsoft sign-in/onboarding operations visible as online."""

        if (
            not self.busy
            or self.shutting_down
            or not self.config
            or self.presence_heartbeat_running
        ):
            return
        self.presence_heartbeat_running = True
        self.next_presence_heartbeat = time.monotonic() + 45
        config = self.config
        accounts = self.store.load()

        def work() -> None:
            succeeded = False
            try:
                send_client_heartbeat(
                    config,
                    account_ids=[account.account_id for account in accounts],
                    status=self._client_control_status(),
                )
                succeeded = True
            except Exception:
                # Presence is best-effort and must never interrupt Microsoft sign-in.
                pass

            def completed() -> None:
                self.presence_heartbeat_running = False
                self.next_presence_heartbeat = time.monotonic() + (45 if succeeded else 90)

            if not self.shutting_down:
                self.root.after(0, completed)

        threading.Thread(target=work, name="token-pool-presence", daemon=True).start()

    def _poll_admin_control(self) -> None:
        if self.control_polling or self.busy or not self.config:
            return
        self.control_polling = True
        config = self.config
        accounts = self.store.load()

        def work() -> None:
            response: dict[str, object] | None = None
            failure: BaseException | None = None
            try:
                flush_admin_command_results(config)
                response = poll_admin_commands(
                    config,
                    account_ids=[account.account_id for account in accounts],
                    status=self._client_control_status(),
                )
            except BaseException as exc:
                failure = exc

            def completed() -> None:
                self.control_polling = False
                if failure is not None:
                    self.next_control_poll = time.monotonic() + 120
                    return
                value = response or {}
                supported = value.get("supported", True) is not False
                try:
                    interval = int(value.get("poll_after_seconds") or (60 if supported else 300))
                except (TypeError, ValueError):
                    interval = 60
                self.next_control_poll = time.monotonic() + min(max(interval, 30), 300)
                client_status = value.get("client_status")
                if isinstance(client_status, dict):
                    self._reconcile_ready_server_accounts(client_status)
                command = value.get("command")
                if isinstance(command, dict):
                    self._execute_admin_command(command)

            if not self.shutting_down:
                self.root.after(0, completed)

        threading.Thread(target=work, name="token-pool-admin-poll", daemon=True).start()

    def _reconcile_ready_server_accounts(self, client_status: dict[str, object]) -> None:
        """Clear a stale MFA upload error when AWS still has a working session."""

        raw_accounts = client_status.get("accounts")
        if not isinstance(raw_accounts, list):
            return
        ready_ids = {
            str(item.get("account_id") or "")
            for item in raw_accounts
            if isinstance(item, dict) and item.get("ready")
        }
        if not ready_ids:
            return
        changed = False
        for account in self.store.load():
            error = str(account.last_error or "")
            if (
                account.account_id in ready_ids
                and not account.pending_upload
                and "AADSTS50078" in error
                and "interactive_mfa" in error
            ):
                account.last_error = None
                self.store.upsert(account)
                self.log(
                    f"AWS confirms {account.username} is still ready; cleared the stale rejected-retry warning."
                )
                changed = True
        if changed:
            self._refresh_table()

    def _execute_admin_command(self, command: dict[str, object]) -> None:
        command_id = str(command.get("command_id") or "")
        command_name = str(command.get("command") or "")
        payload_value = command.get("payload")
        payload = payload_value if isinstance(payload_value, dict) else {}
        if len(command_id) != 32:
            return
        if command_name == "force_renew":
            requested_ids = payload.get("account_ids")
            selected_ids = (
                {str(value) for value in requested_ids}
                if isinstance(requested_ids, list) and requested_ids
                else None
            )
            accounts = [
                account
                for account in self.store.load()
                if is_automatic_work_account(account.username)
                and (selected_ids is None or account.account_id in selected_ids)
            ]
            if not accounts:
                self._finish_admin_command(
                    command_id,
                    succeeded=False,
                    result={"error": "No matching automatic work account is configured."},
                )
                return
            allow_visible = payload.get("interaction") == "allow_visible"
            if allow_visible:
                self.tray.notify(
                    "Administrator requested token renewal",
                    "Microsoft Edge may open if sign-in or MFA is required.",
                )

            def completed(result: object | None, failure: BaseException | None) -> None:
                batch = (
                    result
                    if isinstance(result, RenewalBatchResult)
                    else RenewalBatchResult(permanent_failures=len(accounts))
                )
                succeeded = failure is None and batch.failures == 0 and batch.successes > 0
                self._finish_admin_command(
                    command_id,
                    succeeded=succeeded,
                    result={
                        "successes": batch.successes,
                        "permanent_failures": batch.permanent_failures,
                        "transient_failures": batch.transient_failures,
                        "interaction": "allow_visible" if allow_visible else "silent_only",
                        "error": str(failure)[:500] if failure else None,
                    },
                )

            started = self._background(
                "Administrator-requested renewal...",
                lambda: self._renew_and_upload(
                    accounts,
                    automatic=True,
                    client_event="admin_force_renew",
                    allow_visible=allow_visible,
                ),
                on_complete=completed,
                show_error=False,
            )
            if not started:
                self._finish_admin_command(
                    command_id,
                    succeeded=False,
                    result={"error": "Client was already busy."},
                )
            return

        if command_name == "force_update":
            def update_work() -> dict[str, object]:
                changed, before, after = check_for_update(self.store.root)
                result = {"changed": changed, "before": before, "after": after}
                queue_admin_command_result(
                    command_id=command_id,
                    succeeded=True,
                    result=result,
                )
                flush_admin_command_results(self.config)
                return result

            def update_completed(result: object | None, failure: BaseException | None) -> None:
                if failure is not None:
                    self._finish_admin_command(
                        command_id,
                        succeeded=False,
                        result={"error": str(failure)[:500]},
                    )
                    return
                value = result if isinstance(result, dict) else {}
                if value.get("changed"):
                    self.log(
                        f"Administrator update installed {value.get('after')}; restarting in the notification area..."
                    )
                    try:
                        restart_after_exit(self.store.root)
                    except Exception as exc:
                        self.log(f"Could not restart after administrator update: {exc}")
                        return
                    self.root.after(0, self.shutdown)

            started = self._background(
                "Administrator-requested update...",
                update_work,
                on_complete=update_completed,
                show_error=False,
            )
            if not started:
                self._finish_admin_command(
                    command_id,
                    succeeded=False,
                    result={"error": "Client was already busy."},
                )
            return

        self._finish_admin_command(
            command_id,
            succeeded=False,
            result={"error": f"Unsupported command: {command_name}"},
        )

    def _finish_admin_command(
        self,
        command_id: str,
        *,
        succeeded: bool,
        result: dict[str, object],
    ) -> None:
        queue_admin_command_result(
            command_id=command_id,
            succeeded=succeeded,
            result=result,
        )
        self.next_control_poll = time.monotonic() + 5

        def send() -> None:
            if self.config:
                flush_admin_command_results(self.config)

        threading.Thread(target=send, name="token-pool-admin-result", daemon=True).start()

    def _scheduler_tick(self) -> None:
        if self.shutting_down:
            return
        now = datetime.now().astimezone()
        due = latest_due_slot(now, self.refresh_times)
        if due is not None:
            key = slot_key(due)
            work_accounts = [
                item for item in self.store.load() if is_automatic_work_account(item.username)
            ]
            accounts = [
                item
                for item in work_accounts
                if float(item.last_uploaded_at or 0) < due.timestamp()
            ]
            pending_this_slot = self.automation_state.pending_work_refresh_slot == key
            retry_due = pending_this_slot and _retry_is_due(self.automation_state.next_work_retry_at, now)
            new_slot = key != self.automation_state.last_work_refresh_slot and not pending_this_slot
            stale_failed_result = (
                self.automation_state.last_work_refresh_slot == key
                and self.automation_state.last_work_refresh_result.startswith("failed:")
            )
            if work_accounts and not accounts and (
                new_slot or pending_this_slot or stale_failed_result
            ):
                self.automation_state.last_work_refresh_slot = key
                self.automation_state.pending_work_refresh_slot = ""
                self.automation_state.next_work_retry_at = ""
                self.automation_state.work_retry_count = 0
                self.automation_state.last_work_refresh_result = "satisfied_by_recent_upload"
                self.automation_state.save()
                self.log(
                    f"Scheduled slot {key} already satisfied by a newer successful AWS upload; "
                    "no second Microsoft renewal was started."
                )
            if accounts and (new_slot or retry_due) and not self.busy:
                if new_slot:
                    self.automation_state.work_retry_count = 0
                self.automation_state.pending_work_refresh_slot = key
                self.automation_state.last_work_refresh_result = "started"
                self.automation_state.save()

                def completed(result: object | None, failure: BaseException | None) -> None:
                    batch = (
                        result
                        if isinstance(result, RenewalBatchResult)
                        else RenewalBatchResult(permanent_failures=len(accounts))
                    )
                    transient = batch.waiting_for_network or (
                        failure is not None and _is_transient_failure(failure)
                    )
                    if transient:
                        if self.automation_state.work_retry_count >= MAX_SCHEDULED_NETWORK_RETRIES:
                            self.automation_state.last_work_refresh_slot = key
                            self.automation_state.pending_work_refresh_slot = ""
                            self.automation_state.next_work_retry_at = ""
                            self.automation_state.last_work_refresh_result = (
                                f"network_retry_exhausted:{self.automation_state.work_retry_count}"
                            )
                            self.log(
                                "Scheduled renewal stopped after two network retries; "
                                "the next scheduled slot or a manual refresh can try again."
                            )
                            self.tray.notify(
                                "FMBSM automatic retries stopped",
                                "Network upload failed after two retries. Retry manually later if needed.",
                            )
                        else:
                            self.automation_state.pending_work_refresh_slot = key
                            self.automation_state.work_retry_count += 1
                            retry_seconds = _network_retry_seconds(self.automation_state.work_retry_count)
                            retry_at = datetime.now().astimezone() + timedelta(seconds=retry_seconds)
                            self.automation_state.next_work_retry_at = retry_at.isoformat(timespec="seconds")
                            self.automation_state.last_work_refresh_result = (
                                f"waiting_for_network:{self.automation_state.work_retry_count}"
                            )
                            self.log(f"Scheduled renewal will retry at {retry_at.strftime('%H:%M:%S')}.")
                            if self.automation_state.work_retry_count == 1:
                                self._notify_renewal_result(batch, failure=failure)
                    else:
                        self.automation_state.last_work_refresh_slot = key
                        self.automation_state.pending_work_refresh_slot = ""
                        self.automation_state.next_work_retry_at = ""
                        self.automation_state.work_retry_count = 0
                        self.automation_state.last_work_refresh_result = (
                            f"success:{batch.successes}"
                            if failure is None and batch.failures == 0
                            else f"failed:{batch.failures or 1}"
                        )
                        self._notify_renewal_result(batch, failure=failure)
                    self.automation_state.save()

                action = "retried" if pending_this_slot else "started"
                self.log(f"Scheduled work-account renewal {action} for slot {key}.")
                self._background(
                    "Scheduled renewal...",
                    lambda: self._renew_and_upload(
                        accounts,
                        automatic=True,
                        client_event="scheduled_retry" if pending_this_slot else "scheduled_refresh",
                        scheduled_slot=key,
                    ),
                    on_complete=completed,
                    show_error=False,
                )

        if (
            self.busy
            and time.monotonic() >= getattr(self, "next_presence_heartbeat", 0.0)
            and not getattr(self, "presence_heartbeat_running", False)
        ):
            self._send_busy_heartbeat()
        if (
            time.monotonic() >= getattr(self, "next_control_poll", float("inf"))
            and not self.busy
            and not getattr(self, "control_polling", False)
        ):
            self._poll_admin_control()
        if (
            time.monotonic() >= self.next_update_check
            and not self.busy
            and not getattr(self, "control_polling", False)
        ):
            self.check_updates(manual=False)
        poll_seconds = max(2, int(os.getenv("TOKEN_POOL_SCHEDULER_POLL_SECONDS", "30")))
        self.root.after(poll_seconds * 1000, self._scheduler_tick)

    def check_updates(self, *, manual: bool) -> None:
        self.next_update_check = time.monotonic() + self.update_interval

        def work() -> tuple[bool, str, str]:
            self.log("Checking GitHub for a Token Pool Client update...")
            return check_for_update(self.store.root)

        def completed(result: object | None, failure: BaseException | None) -> None:
            self.automation_state.last_update_check_at = datetime.now().astimezone().isoformat(timespec="seconds")
            self.automation_state.save()
            if failure is not None:
                if manual:
                    self.tray.notify("Update check failed", str(failure))
                return
            changed, before, after = result if isinstance(result, tuple) else (False, "", "")
            if changed:
                self.log(f"Updated from {before} to {after}; restarting in the notification area...")
                self.tray.notify("FMBSM client updated", f"Installed {after}; restarting now.")
                try:
                    restart_after_exit(self.store.root)
                except Exception as exc:
                    self.log(f"Could not restart after update: {exc}")
                    return
                self.root.after(0, self.shutdown)
            else:
                self.log("No client update is available.")
                if manual:
                    self.tray.notify("FMBSM client", "The Token Pool Client is already up to date.")

        self._background("Checking for updates...", work, on_complete=completed, show_error=manual)

    def hide_to_tray(self) -> None:
        self.root.withdraw()
        self.tray.notify("FMBSM Token Pool Client", "Still running in the notification area for scheduled renewal.")

    def show_window(self) -> None:
        if self.shutting_down:
            return
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()

    def shutdown(self) -> None:
        if self.shutting_down:
            return
        self.shutting_down = True
        self.instance.close()
        self.tray.stop()
        self.root.destroy()


def _time_text(value: float | None) -> str:
    if not value:
        return "—"
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")


def _my_aws_status_text(
    local_accounts: list[ClientAccount],
    response: dict[str, object],
) -> str:
    summary_value = response.get("summary")
    summary = summary_value if isinstance(summary_value, dict) else {}
    remote_value = response.get("accounts")
    remote_accounts = {
        str(account.get("account_id") or ""): account
        for account in (remote_value if isinstance(remote_value, list) else [])
        if isinstance(account, dict)
    }
    lines = [
        "AWS connection: Online",
        f"Server API: {response.get('version') or 'reachable'}",
        (
            "Your accounts ready on AWS: "
            f"{summary.get('ready_account_count', 0)} / "
            f"{summary.get('configured_account_count', len(local_accounts))}"
        ),
    ]
    if not local_accounts:
        lines.append("No Microsoft account is configured in this app yet.")
    for account in local_accounts:
        remote = remote_accounts.get(account.account_id)
        if not remote or not remote.get("uploaded"):
            lines.append(f"{account.username}: Not uploaded to AWS")
            continue
        state = str(remote.get("state") or "renewal_required")
        if remote.get("ready"):
            label = "Uploaded and ready"
        elif state == "cooldown":
            label = "Uploaded; temporarily on cooldown"
        elif state == "disabled":
            label = "Uploaded; disabled on AWS"
        else:
            label = "Uploaded; Microsoft renewal required"
        lines.append(
            f"{account.username}: {label} "
            f"(uploaded {_time_text(remote.get('uploaded_at'))}; "
            f"authorization valid until {_time_text(remote.get('authorization_expires_at'))})"
        )
    return "\n".join(lines)


def _authorization_expires_at(account: ClientAccount) -> float | None:
    if account.authorization_expires_at:
        return account.authorization_expires_at
    if account.last_uploaded_at:
        return account.last_uploaded_at + 24 * 60 * 60
    if account.access_expires_at:
        # Existing v1.0.10 records predate this field. Their one-hour access
        # token was created at the start of the fixed 24-hour authorization.
        return account.access_expires_at + 23 * 60 * 60
    return None


def _is_transient_failure(error: BaseException) -> bool:
    return isinstance(error, bootstrap.BrowserConnectivityError) or is_transient_network_error(error)


def _account_status(account: ClientAccount, *, now: float | None = None) -> str:
    current = time.time() if now is None else now
    expires_at = _authorization_expires_at(account)
    if not expires_at or expires_at <= current + 300:
        return "Microsoft sign-in renewal due"
    if account.last_error:
        if _is_transient_failure(RuntimeError(account.last_error)):
            return "Waiting for network; retry scheduled"
        return account.last_error
    return "Ready"


def _server_account_state(account: dict[str, object], *, now: float) -> str:
    if not account.get("enabled", True):
        return "disabled"
    if account.get("cooling_down"):
        return "cooldown"
    runtime_available = account.get("runtime_available")
    if runtime_available is None:
        refresh_expires_at = account.get("refresh_expires_at")
        if refresh_expires_at is None:
            refresh_potentially_valid = True
        else:
            try:
                refresh_potentially_valid = float(refresh_expires_at) > now
            except (TypeError, ValueError):
                refresh_potentially_valid = False
        runtime_available = bool(account.get("access_valid")) or refresh_potentially_valid
    return "ready" if runtime_available else "Microsoft sign-in renewal due"


def _retry_is_due(value: str, now: datetime) -> bool:
    if not value:
        return True
    try:
        retry_at = datetime.fromisoformat(value)
    except ValueError:
        return True
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=now.tzinfo)
    return now >= retry_at


def _network_retry_seconds(retry_count: int) -> int:
    base = max(30, int(os.getenv("TOKEN_POOL_NETWORK_RETRY_SECONDS", "300")))
    return min(15 * 60, base * (2 ** min(max(0, retry_count - 1), 2)))


def run(*, background: bool = False) -> None:
    root = tk.Tk()
    holder: dict[str, TokenPoolApp] = {}

    def on_second_launch() -> None:
        root.after(0, lambda: holder.get("app") and holder["app"].show_window())

    instance = SingleInstance(on_second_launch)
    if not instance.acquire():
        root.destroy()
        return
    app = TokenPoolApp(root, background=background, instance=instance)
    holder["app"] = app
    try:
        root.mainloop()
    finally:
        if not app.shutting_down:
            instance.close()
            app.tray.stop()
