from __future__ import annotations

import base64
import os
import shutil
import sys
import threading
import time
import uuid
from datetime import datetime
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
from .refresh import initialize_refresh
from .storage import AccountStore, ClientAccount
from .tray import NotificationTray
from .upload import ClientConfig, load_config, server_status, upload_bundle


SAMPLE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAV0lEQVR4nO3PQQ0AIBDAsAP/nuGNAvZoFSzZOjNnyNi1dwfgUQFeFeBVAV4V4FUBXhXgVQFeFeBVAV4V4FUBXhXgVQFeFeBVAV4V4FUBXhXgVQFeFeBVAV4V4FUB3gA5ggJ/QlTzGAAAAABJRU5ErkJggg=="
)


class TokenPoolApp:
    def __init__(self, root: tk.Tk, *, background: bool, instance: SingleInstance) -> None:
        self.root = root
        self.background = background
        self.instance = instance
        self.store = AccountStore()
        self.config: ClientConfig | None = None
        self.busy = False
        self.shutting_down = False
        self.refresh_times = configured_refresh_times()
        self.update_interval = update_interval_seconds()
        self.automation_state = AutomationState.load(self.store.root)
        self.next_update_check = time.monotonic() + self.update_interval
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
        self.status_button = ttk.Button(controls, text="Server status", command=self.show_status)
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
            ("expires", "Access token expires", 155),
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
            expires = _time_text(account.access_expires_at)
            uploaded = _time_text(account.last_uploaded_at) if account.last_uploaded_at else "Never"
            status = account.last_error or ("Ready" if account.access_expires_at > now + 300 else "Renewal required")
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
        state = "disabled" if busy else "normal"
        for button in (self.refresh_button, self.add_button, self.status_button):
            button.configure(state=state)
        self.state_label.configure(text=text)

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

    def _fresh_renewal(self, account: ClientAccount, *, automatic: bool) -> ClientAccount:
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

    def _renew_and_upload(self, accounts: list[ClientAccount], *, automatic: bool) -> tuple[int, int]:
        if not self.config:
            raise RuntimeError("Client configuration is unavailable")
        successes = 0
        failures = 0
        for account in accounts:
            try:
                renewed = self._fresh_renewal(account, automatic=automatic)
                response = upload_bundle(self.config, create_bundle(renewed))
                renewed.last_uploaded_at = time.time()
                renewed.last_error = None
                self.store.upsert(renewed)
                available = response.get("pool", {}).get("available_account_count")
                self.log(f"Uploaded {renewed.username}; server pool available={available}")
                successes += 1
            except Exception as exc:
                account.last_error = str(exc)[:300]
                self.store.upsert(account)
                self.log(f"Failed {account.username}: {exc}")
                failures += 1
        return successes, failures

    def refresh_all(self) -> None:
        accounts = self.store.load()
        if not accounts:
            self.log("No account is configured. Choose Add Microsoft account first.")
            return
        self._background(
            "Renewing all accounts...",
            lambda: self._renew_and_upload(accounts, automatic=False),
        )

    def refresh_work_now(self) -> None:
        accounts = [account for account in self.store.load() if is_automatic_work_account(account.username)]
        if not accounts:
            self.log("No @forvismazars.com or @mazars.fr work account is configured for automatic renewal.")
            return

        def completed(result: object | None, failure: BaseException | None) -> None:
            successes, failures = result if isinstance(result, tuple) else (0, len(accounts))
            if failure is not None or failures:
                self.tray.notify(
                    "Microsoft sign-in required",
                    "Your work-account token was not renewed. Open Token Pool Client and complete Microsoft sign-in.",
                )
            elif successes:
                self.tray.notify("FMBSM token renewed", "Your work-account session was refreshed and uploaded successfully.")

        self._background(
            "Renewing work account...",
            lambda: self._renew_and_upload(accounts, automatic=True),
            on_complete=completed,
            show_error=False,
        )

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
                final_session, final_profile = self.store.account_paths(account.account_id)
                shutil.copytree(session, final_session, dirs_exist_ok=True)
                shutil.copytree(profile, final_profile, dirs_exist_ok=True)
                account.session_dir = str(final_session)
                account.profile_dir = str(final_profile)
                response = upload_bundle(self.config, create_bundle(account))
                account.last_uploaded_at = time.time()
                self.store.upsert(account)
                self.log(
                    f"Added and uploaded {account.username}; "
                    f"pool available={response.get('pool', {}).get('available_account_count')}"
                )
            finally:
                bootstrap.set_interaction_callback(None)
                shutil.rmtree(onboarding, ignore_errors=True)

        self._background("Adding account...", work)

    def show_status(self) -> None:
        if not self.config:
            return

        def work() -> None:
            response = server_status(self.config)
            pool = response.get("pool", {})
            lines = [
                f"Accounts: {pool.get('account_count', 0)}",
                f"Available: {pool.get('available_account_count', 0)}",
                f"Turns in the last hour: {pool.get('recent_turns', 0)}",
            ]
            for account in pool.get("accounts", []):
                state = "cooldown" if account.get("cooling_down") else ("ready" if account.get("access_valid") else "refreshing/expired")
                lines.append(f"{account.get('username')}: {state}, total turns={account.get('total_turns')}")
            text = "\n".join(lines)
            self.log(text.replace("\n", " | "))
            self.root.after(0, lambda: messagebox.showinfo("AWS Copilot pool", text))

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

    def _scheduler_tick(self) -> None:
        if self.shutting_down:
            return
        now = datetime.now().astimezone()
        due = latest_due_slot(now, self.refresh_times)
        if due is not None:
            key = slot_key(due)
            has_work_account = any(is_automatic_work_account(item.username) for item in self.store.load())
            if has_work_account and key != self.automation_state.last_work_refresh_slot and not self.busy:
                self.automation_state.last_work_refresh_slot = key
                self.automation_state.last_work_refresh_result = "started"
                self.automation_state.save()

                def completed(result: object | None, failure: BaseException | None) -> None:
                    successes, failures = result if isinstance(result, tuple) else (0, 1)
                    self.automation_state.last_work_refresh_result = (
                        f"success:{successes}" if failure is None and failures == 0 else f"failed:{failures}"
                    )
                    self.automation_state.save()
                    if failure is not None or failures:
                        self.tray.notify(
                            "Microsoft sign-in required",
                            "Your work-account token was not renewed. Open Token Pool Client and complete Microsoft sign-in.",
                        )
                    else:
                        self.tray.notify("FMBSM token renewed", "Your work-account session was refreshed and uploaded successfully.")

                self.log(f"Scheduled work-account renewal started for slot {key}.")
                accounts = [item for item in self.store.load() if is_automatic_work_account(item.username)]
                self._background(
                    "Scheduled renewal...",
                    lambda: self._renew_and_upload(accounts, automatic=True),
                    on_complete=completed,
                    show_error=False,
                )

        if time.monotonic() >= self.next_update_check and not self.busy:
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
