from __future__ import annotations

import base64
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
from .bundle import create_bundle
from .refresh import (
    RefreshError,
    initialize_refresh,
    refresh_existing,
    requires_interactive_reauthentication,
)
from .storage import AccountStore, ClientAccount
from .upload import ClientConfig, load_config, server_status, upload_bundle


SAMPLE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAV0lEQVR4nO3PQQ0AIBDAsAP/nuGNAvZoFSzZOjNnyNi1dwfgUQFeFeBVAV4V4FUBXhXgVQFeFeBVAV4V4FUBXhXgVQFeFeBVAV4V4FUBXhXgVQFeFeBVAV4V4FUB3gA5ggJ/QlTzGAAAAABJRU5ErkJggg=="
)


class TokenPoolApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.store = AccountStore()
        self.config: ClientConfig | None = None
        self.busy = False
        self.root.title(f"FMBSM Token Pool Client {__version__}")
        self.root.geometry("920x620")
        self.root.minsize(780, 520)
        self.root.configure(bg="#f3f6f8")
        self.logo = self._load_logo()
        if self.logo is not None:
            self.root.iconphoto(True, self.logo)
        self._style()
        self._build()
        self._load_config()
        self._refresh_table()
        if self.store.load() and self.config:
            self.root.after(900, self.refresh_all)

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

    def _load_logo(self) -> tk.PhotoImage | None:
        candidates = []
        if getattr(sys, "frozen", False):
            candidates.append(Path(sys.executable).resolve().parent / "token-pool-logo.png")
        candidates.append(Path(__file__).resolve().parents[2] / "assets" / "token-pool-logo.png")
        for path in candidates:
            try:
                return tk.PhotoImage(file=str(path))
            except Exception:
                continue
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
            text="Renew your Microsoft sign-in and contribute it securely to the AWS automation pool.",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        controls = ttk.Frame(self.root)
        controls.pack(fill=X, padx=22, pady=(18, 10))
        self.refresh_button = ttk.Button(controls, text="Renew sign-in & upload all", style="Primary.TButton", command=self.refresh_all)
        self.refresh_button.pack(side=LEFT)
        self.add_button = ttk.Button(controls, text="Add Microsoft account", command=self.add_account)
        self.add_button.pack(side=LEFT, padx=8)
        self.status_button = ttk.Button(controls, text="Server status", command=self.show_status)
        self.status_button.pack(side=LEFT)
        self.state_label = ttk.Label(controls, text="Ready", font=("Segoe UI Semibold", 10))
        self.state_label.pack(side=RIGHT, padx=5)

        card = ttk.Frame(self.root, style="Card.TFrame")
        card.pack(fill=BOTH, expand=True, padx=22, pady=(0, 12))
        columns = ("account", "expires", "uploaded", "status")
        self.table = ttk.Treeview(card, columns=columns, show="headings", height=8)
        for column, label, width in (
            ("account", "Microsoft account", 280),
            ("expires", "Access token expires", 170),
            ("uploaded", "Last uploaded", 170),
            ("status", "Status", 220),
        ):
            self.table.heading(column, text=label)
            self.table.column(column, width=width, minwidth=120, anchor="w")
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
            messagebox.showerror("Configuration missing", str(exc))

    def _refresh_table(self) -> None:
        for item in self.table.get_children():
            self.table.delete(item)
        now = time.time()
        for account in self.store.load():
            expires = _time_text(account.access_expires_at)
            uploaded = _time_text(account.last_uploaded_at) if account.last_uploaded_at else "Never"
            status = account.last_error or ("Ready" if account.access_expires_at > now + 300 else "Refresh required")
            self.table.insert("", END, iid=account.account_id, values=(account.username, expires, uploaded, status))

    def log(self, message: str) -> None:
        def append() -> None:
            timestamp = datetime.now().strftime("%H:%M:%S")
            self.log_box.configure(state="normal")
            self.log_box.insert(END, f"[{timestamp}] {message}\n")
            self.log_box.see(END)
            self.log_box.configure(state="disabled")

        self.root.after(0, append)

    def _set_busy(self, busy: bool, text: str = "Ready") -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        for button in (self.refresh_button, self.add_button, self.status_button):
            button.configure(state=state)
        self.state_label.configure(text=text)

    def _background(self, label: str, work: Callable[[], None]) -> None:
        if self.busy:
            return
        self._set_busy(True, label)

        def runner() -> None:
            try:
                work()
            except Exception as exc:
                self.log(f"ERROR: {exc}")
                self.root.after(0, lambda: messagebox.showerror("Token Pool Client", str(exc)))
            finally:
                self.root.after(0, self._refresh_table)
                self.root.after(0, lambda: self._set_busy(False))

        threading.Thread(target=runner, name="token-pool-work", daemon=True).start()

    def _browser_interaction(self, message: str) -> None:
        event = threading.Event()

        def show() -> None:
            messagebox.showinfo("Microsoft sign-in required", message)
            event.set()

        self.root.after(0, show)
        event.wait()

    def _interactive_renewal(self, account: ClientAccount) -> ClientAccount:
        bootstrap.set_interaction_callback(self._browser_interaction)
        try:
            bootstrap.renew_microsoft_session(
                site="https://m365.cloud.microsoft/chat",
                profile_dir=account.profile_path,
                session_dir=account.session_path,
                expected_username=account.username,
                expected_tenant=account.tenant_id,
                channel="msedge",
                timeout_seconds=300,
                progress=self.log,
            )
            renewed, _ = initialize_refresh(
                account.session_path,
                account.profile_path,
                expected_account=account,
            )
            renewed.last_uploaded_at = account.last_uploaded_at
            return renewed
        finally:
            bootstrap.set_interaction_callback(None)

    def refresh_all(self) -> None:
        if not self.config:
            return

        def work() -> None:
            accounts = self.store.load()
            if not accounts:
                self.log("No account is configured. Choose Add Microsoft account first.")
                return
            for account in accounts:
                self.log(f"Renewing Microsoft session for {account.username}...")
                try:
                    try:
                        refreshed, _ = refresh_existing(account)
                        self.log(f"Silent Microsoft token renewal succeeded for {account.username}.")
                    except RefreshError as exc:
                        if not requires_interactive_reauthentication(exc):
                            raise
                        self.log(
                            f"The 24-hour Microsoft sign-in expired for {account.username}; "
                            "opening Edge for a real sign-in/MFA..."
                        )
                        refreshed = self._interactive_renewal(account)
                        self.log(f"New Microsoft sign-in captured for {refreshed.username}.")
                    response = upload_bundle(self.config, create_bundle(refreshed))
                    refreshed.last_uploaded_at = time.time()
                    refreshed.last_error = None
                    self.store.upsert(refreshed)
                    available = response.get("pool", {}).get("available_account_count")
                    self.log(f"Uploaded {refreshed.username}; server pool available={available}")
                except Exception as exc:
                    account.last_error = str(exc)[:300]
                    self.store.upsert(account)
                    self.log(f"Failed {account.username}: {exc}")

        self._background("Refreshing...", work)

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


def _time_text(value: float | None) -> str:
    if not value:
        return "—"
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")


def run() -> None:
    root = tk.Tk()
    TokenPoolApp(root)
    root.mainloop()
