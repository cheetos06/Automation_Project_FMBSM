from __future__ import annotations

import json
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Callable

from . import __version__
from .api import cancel_command, create_commands, forget_clients, snapshot, start_copilot_test
from .storage import AdminConfig, executable_dir, load_config


BG = "#edf2f5"
NAVY = "#103b4a"
TEAL = "#0f7c8c"
GREEN = "#19764a"
AMBER = "#a86700"
RED = "#b23838"
MUTED = "#60737b"


class TokenPoolAdminApp:
    def __init__(self, root: tk.Tk, config: AdminConfig) -> None:
        self.root = root
        self.config = config
        self.snapshot_data: dict[str, Any] = {}
        self.refreshing = False
        self.closed = False
        self.auto_refresh = tk.BooleanVar(value=True)
        self.interaction_mode = tk.StringVar(value="Silent only — never open Edge")
        self.status_text = tk.StringVar(value="Connecting…")
        self.client_items: dict[str, dict[str, Any]] = {}
        self.account_items: dict[str, dict[str, Any]] = {}
        self.command_items: dict[str, dict[str, Any]] = {}

        root.title(f"FMBSM Token Pool Admin {__version__}")
        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()
        window_width = min(1380, max(1000, screen_width - 60))
        window_height = min(850, max(650, screen_height - 90))
        root.geometry(f"{window_width}x{window_height}")
        root.minsize(1000, 650)
        root.configure(bg=BG)
        self._set_icon()
        self._configure_styles()
        self._build()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(250, self.refresh)
        root.after(10_000, self._auto_refresh_tick)

    def _set_icon(self) -> None:
        candidates = [
            executable_dir() / "token-pool-logo.png",
            Path(__file__).resolve().parents[3] / "token-pool-client" / "assets" / "token-pool-logo.png",
        ]
        for path in candidates:
            if not path.exists():
                continue
            try:
                self.logo = tk.PhotoImage(file=str(path))
                self.root.iconphoto(True, self.logo)
                return
            except tk.TclError:
                continue

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground="#1a2c33", font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI Semibold", 10), padding=(12, 8))
        style.configure("Primary.TButton", background=TEAL, foreground="white")
        style.map("Primary.TButton", background=[("active", "#0b6572"), ("disabled", "#90abb1")])
        style.configure("Danger.TButton", background="#f6e7e7", foreground=RED)
        style.configure("Treeview", rowheight=29, font=("Segoe UI", 9), background="white", fieldbackground="white")
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 9), padding=(5, 7))
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", font=("Segoe UI Semibold", 10), padding=(18, 9))

    def _build(self) -> None:
        header = tk.Frame(self.root, bg=NAVY, height=104)
        header.pack(fill="x")
        header.pack_propagate(False)
        header.columnconfigure(0, weight=1)
        title_area = tk.Frame(header, bg=NAVY)
        title_area.grid(row=0, column=0, sticky="nsew", padx=(26, 12))
        tk.Label(
            title_area,
            text="FMBSM Control Center",
            bg=NAVY,
            fg="white",
            font=("Segoe UI Semibold", 23),
        ).pack(anchor="w", pady=(18, 0))
        tk.Label(
            title_area,
            text="Private administration for desktop contributors, Copilot capacity, and AWS health",
            bg=NAVY,
            fg="#c9e1e7",
            font=("Segoe UI", 10),
            wraplength=610,
            justify="left",
        ).pack(anchor="w", pady=(2, 0))

        actions = tk.Frame(header, bg=NAVY)
        actions.grid(row=0, column=1, sticky="e", padx=(8, 24))
        ttk.Button(actions, text="Refresh now", style="Primary.TButton", command=self.refresh).pack(side="left", padx=5)
        ttk.Checkbutton(actions, text="Auto-refresh", variable=self.auto_refresh).pack(side="left", padx=8)
        tk.Label(
            actions,
            textvariable=self.status_text,
            bg=NAVY,
            fg="#d5e8ec",
            width=22,
            anchor="e",
            font=("Segoe UI", 9),
        ).pack(side="left", padx=(8, 0))

        content = ttk.Frame(self.root)
        content.pack(fill="both", expand=True, padx=20, pady=16)
        self.cards = ttk.Frame(content)
        self.cards.pack(fill="x", pady=(0, 12))
        for index in range(4):
            self.cards.columnconfigure(index, weight=1, uniform="card")
        self.card_server = self._card(self.cards, 0, "AWS SERVER")
        self.card_clients = self._card(self.cards, 1, "CLIENTS ONLINE")
        self.card_pool = self._card(self.cards, 2, "COPILOT POOL")
        self.card_test = self._card(self.cards, 3, "LATEST HEALTH TEST")

        self.notebook = ttk.Notebook(content)
        self.notebook.pack(fill="both", expand=True)
        self.clients_tab = ttk.Frame(self.notebook)
        self.accounts_tab = ttk.Frame(self.notebook)
        self.operations_tab = ttk.Frame(self.notebook)
        self.server_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.clients_tab, text="Desktop clients")
        self.notebook.add(self.accounts_tab, text="Copilot accounts")
        self.notebook.add(self.operations_tab, text="Commands & tests")
        self.notebook.add(self.server_tab, text="Server details")
        self._build_clients_tab()
        self._build_accounts_tab()
        self._build_operations_tab()
        self._build_server_tab()

    def _card(self, parent: ttk.Frame, column: int, label: str) -> dict[str, tk.Label]:
        frame = tk.Frame(parent, bg="white", highlightbackground="#d9e2e6", highlightthickness=1)
        frame.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 5, 0 if column == 3 else 5))
        tk.Label(frame, text=label, bg="white", fg=MUTED, font=("Segoe UI Semibold", 8)).pack(anchor="w", padx=15, pady=(12, 0))
        value = tk.Label(frame, text="—", bg="white", fg=NAVY, font=("Segoe UI Semibold", 19))
        value.pack(anchor="w", padx=15, pady=(1, 0))
        detail = tk.Label(frame, text="Waiting for data", bg="white", fg=MUTED, font=("Segoe UI", 8))
        detail.pack(anchor="w", padx=15, pady=(0, 12))
        return {"value": value, "detail": detail}

    def _build_clients_tab(self) -> None:
        bar = ttk.Frame(self.clients_tab)
        bar.pack(fill="x", padx=10, pady=10)
        ttk.Label(bar, text="Renewal behavior:").pack(side="left")
        mode = ttk.Combobox(
            bar,
            textvariable=self.interaction_mode,
            values=("Silent only — never open Edge", "Allow Edge / sign-in / MFA"),
            state="readonly",
            width=31,
        )
        mode.pack(side="left", padx=7)
        ttk.Button(bar, text="Force renewal", style="Primary.TButton", command=self.force_renew).pack(side="left", padx=4)
        ttk.Button(bar, text="Force update check", command=self.force_update).pack(side="left", padx=4)
        ttk.Button(bar, text="Copy diagnostics", command=self.copy_client_diagnostics).pack(side="left", padx=4)
        ttk.Button(
            bar,
            text="Forget offline unmapped",
            style="Danger.TButton",
            command=self.forget_selected_clients,
        ).pack(side="left", padx=4)

        columns = ("online", "accounts", "version", "last_seen", "activity", "token", "commands")
        self.clients_tree = self._tree(
            self.clients_tab,
            columns,
            (
                ("online", "Status", 85),
                ("accounts", "User / accounts", 270),
                ("version", "Client version", 165),
                ("last_seen", "Last seen", 145),
                ("activity", "Current activity", 180),
                ("token", "Token state", 215),
                ("commands", "Pending", 70),
            ),
            selectmode="extended",
        )

    def _build_accounts_tab(self) -> None:
        bar = ttk.Frame(self.accounts_tab)
        bar.pack(fill="x", padx=10, pady=10)
        ttk.Button(bar, text="Ping selected", style="Primary.TButton", command=self.ping_selected).pack(side="left", padx=4)
        ttk.Button(bar, text="Ping all accounts", command=self.ping_all).pack(side="left", padx=4)
        ttk.Label(
            bar,
            text="Each ping sends one minimal real prompt and consumes one recorded Copilot turn.",
            foreground=MUTED,
        ).pack(side="right")
        columns = (
            "account",
            "runtime",
            "access",
            "authorization",
            "cooldown",
            "hour_turns",
            "day_turns",
            "lifetime_turns",
            "uploaded",
            "error",
        )
        self.accounts_tree = self._tree(
            self.accounts_tab,
            columns,
            (
                ("account", "Copilot account", 245),
                ("runtime", "Runtime", 95),
                ("access", "Access token", 135),
                ("authorization", "Authorization", 145),
                ("cooldown", "Cooldown", 105),
                ("hour_turns", "Last hour", 75),
                ("day_turns", "Last 24h", 75),
                ("lifetime_turns", "Lifetime", 75),
                ("uploaded", "Last upload", 140),
                ("error", "Last error", 300),
            ),
            selectmode="extended",
        )

    def _build_operations_tab(self) -> None:
        pane = ttk.Panedwindow(self.operations_tab, orient="vertical")
        pane.pack(fill="both", expand=True, padx=10, pady=10)
        commands_frame = ttk.Frame(pane)
        tests_frame = ttk.Frame(pane)
        pane.add(commands_frame, weight=2)
        pane.add(tests_frame, weight=2)
        command_bar = ttk.Frame(commands_frame)
        command_bar.pack(fill="x", pady=(0, 6))
        ttk.Label(command_bar, text="Remote command audit", font=("Segoe UI Semibold", 11)).pack(side="left")
        ttk.Button(command_bar, text="Cancel selected pending command", style="Danger.TButton", command=self.cancel_selected_command).pack(side="right")
        self.commands_tree = self._tree(
            commands_frame,
            ("created", "client", "command", "mode", "status", "attempts", "result"),
            (
                ("created", "Created", 135),
                ("client", "Client / user", 235),
                ("command", "Command", 125),
                ("mode", "Mode", 110),
                ("status", "Status", 100),
                ("attempts", "Attempts", 65),
                ("result", "Result", 380),
            ),
            selectmode="browse",
            pack=False,
        )
        self.commands_tree.master.pack(fill="both", expand=True)

        ttk.Label(tests_frame, text="Copilot health-test history", font=("Segoe UI Semibold", 11)).pack(anchor="w", pady=(8, 6))
        self.tests_tree = self._tree(
            tests_frame,
            ("started", "stage", "progress", "message", "results"),
            (
                ("started", "Started", 135),
                ("stage", "Stage", 135),
                ("progress", "Progress", 85),
                ("message", "Live progress", 400),
                ("results", "Results", 390),
            ),
            selectmode="browse",
            pack=False,
        )
        self.tests_tree.master.pack(fill="both", expand=True)

    def _build_server_tab(self) -> None:
        self.server_text = tk.Text(
            self.server_tab,
            bg="#14272e",
            fg="#d9edf0",
            insertbackground="white",
            relief="flat",
            font=("Cascadia Mono", 10),
            padx=18,
            pady=15,
            wrap="word",
        )
        self.server_text.pack(fill="both", expand=True, padx=10, pady=10)
        self.server_text.configure(state="disabled")

    def _tree(
        self,
        parent: ttk.Frame,
        columns: tuple[str, ...],
        definitions: tuple[tuple[str, str, int], ...],
        *,
        selectmode: str,
        pack: bool = True,
    ) -> ttk.Treeview:
        holder = ttk.Frame(parent)
        tree = ttk.Treeview(holder, columns=columns, show="headings", selectmode=selectmode)
        vertical = ttk.Scrollbar(holder, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(holder, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)
        for key, label, width in definitions:
            tree.heading(key, text=label)
            tree.column(key, width=width, minwidth=55, anchor="w", stretch=True)
        tree.tag_configure("online", foreground=GREEN)
        tree.tag_configure("offline", foreground=MUTED)
        tree.tag_configure("warning", foreground=AMBER)
        tree.tag_configure("failed", foreground=RED)
        if pack:
            holder.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        return tree

    def refresh(self) -> None:
        if self.refreshing or self.closed:
            return
        self.refreshing = True
        self.status_text.set("Refreshing…")

        def work() -> None:
            try:
                value = snapshot(self.config)
            except BaseException as exc:
                self.root.after(0, lambda error=exc: self._refresh_failed(error))
                return
            self.root.after(0, lambda: self._apply_snapshot(value))

        threading.Thread(target=work, name="token-admin-refresh", daemon=True).start()

    def _refresh_failed(self, error: BaseException) -> None:
        self.refreshing = False
        self.status_text.set("Server unavailable")
        self.card_server["value"].configure(text="OFFLINE", fg=RED)
        self.card_server["detail"].configure(text=str(error)[:80])

    def _apply_snapshot(self, value: dict[str, Any]) -> None:
        self.snapshot_data = value
        self.refreshing = False
        self.status_text.set("Updated " + datetime.now().strftime("%H:%M:%S"))
        self._update_cards(value)
        self._update_clients(value)
        self._update_accounts(value)
        self._update_operations(value)
        self._update_server(value)

    def _update_cards(self, value: dict[str, Any]) -> None:
        server = value.get("server") if isinstance(value.get("server"), dict) else {}
        clients = value.get("clients") if isinstance(value.get("clients"), list) else []
        pool = value.get("pool") if isinstance(value.get("pool"), dict) else {}
        tests = value.get("copilot_tests") if isinstance(value.get("copilot_tests"), list) else []
        online = sum(1 for client in clients if isinstance(client, dict) and client.get("online"))
        self.card_server["value"].configure(text="ONLINE", fg=GREEN)
        self.card_server["detail"].configure(text=f"System uptime {_duration(server.get('system_uptime_seconds'))}")
        self.card_clients["value"].configure(text=f"{online} / {len(clients)}", fg=GREEN if online else AMBER)
        self.card_clients["detail"].configure(text="Seen during the last 150 seconds")
        available = int(pool.get("available_account_count") or 0)
        total = int(pool.get("account_count") or 0)
        self.card_pool["value"].configure(text=f"{available} / {total}", fg=GREEN if available == total and total else AMBER)
        hour_turns = int(pool.get("turns_last_hour") or pool.get("recent_turns") or 0)
        day_turns = int(pool.get("turns_last_24_hours") or 0)
        self.card_pool["detail"].configure(
            text=f"{hour_turns} last hour · {day_turns} last 24h"
        )
        if tests:
            latest = tests[0] if isinstance(tests[0], dict) else {}
            progress = latest.get("progress") if isinstance(latest.get("progress"), dict) else {}
            results = latest.get("results") if isinstance(latest.get("results"), list) else []
            passed = sum(1 for result in results if isinstance(result, dict) and result.get("ok"))
            total_results = int(progress.get("total") or len(results))
            self.card_test["value"].configure(text=f"{passed} / {total_results}", fg=GREEN if passed == total_results and total_results else AMBER)
            self.card_test["detail"].configure(text=str(latest.get("stage") or "unknown").replace("_", " ").title())
        else:
            self.card_test["value"].configure(text="NOT RUN", fg=MUTED)
            self.card_test["detail"].configure(text="Select accounts and run a ping")

    def _update_clients(self, value: dict[str, Any]) -> None:
        selected = set(self.clients_tree.selection())
        self.clients_tree.delete(*self.clients_tree.get_children())
        self.client_items.clear()
        clients = value.get("clients") if isinstance(value.get("clients"), list) else []
        for client in clients:
            if not isinstance(client, dict):
                continue
            client_id = str(client.get("client_id") or "")
            names = client.get("account_usernames") if isinstance(client.get("account_usernames"), list) else []
            status = client.get("status") if isinstance(client.get("status"), dict) else {}
            server_accounts = (
                client.get("server_accounts")
                if isinstance(client.get("server_accounts"), list)
                else []
            )
            state_labels = {
                "ready": "Ready on AWS",
                "cooldown": "Ready on AWS (cooldown)",
                "renewal_required": "Sign-in renewal due",
                "not_uploaded": "Not uploaded to AWS",
                "disabled": "Disabled on AWS",
            }
            token_state = "; ".join(
                state_labels.get(str(item.get("state") or ""), "Unknown on AWS")
                for item in server_accounts
                if isinstance(item, dict)
            ) or "No Microsoft account configured"
            online = bool(client.get("online"))
            activity = (
                str(status.get("activity") or "Busy")
                if status.get("busy")
                else str(client.get("last_event") or "Idle").replace("_", " ").title()
            )
            iid = "client-" + client_id
            self.clients_tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    "Online" if online else "Offline",
                    ", ".join(str(name) for name in names) or "Unmapped installation",
                    client.get("app_version") or "Unknown",
                    _time_text(client.get("last_seen_at")),
                    activity,
                    token_state,
                    client.get("active_command_count") or 0,
                ),
                tags=("online" if online else "offline",),
            )
            self.client_items[iid] = client
            if iid in selected:
                self.clients_tree.selection_add(iid)

    def _update_accounts(self, value: dict[str, Any]) -> None:
        selected = set(self.accounts_tree.selection())
        self.accounts_tree.delete(*self.accounts_tree.get_children())
        self.account_items.clear()
        pool = value.get("pool") if isinstance(value.get("pool"), dict) else {}
        accounts = pool.get("accounts") if isinstance(pool.get("accounts"), list) else []
        for account in accounts:
            if not isinstance(account, dict):
                continue
            account_id = str(account.get("account_id") or "")
            runtime = bool(account.get("runtime_available"))
            cooling = bool(account.get("cooling_down"))
            tag = "failed" if not runtime else ("warning" if cooling else "online")
            iid = "account-" + account_id
            self.accounts_tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    account.get("username") or account_id,
                    "Working" if runtime else "Unavailable",
                    _remaining(account.get("access_expires_at"), value.get("now")),
                    _remaining(account.get("refresh_expires_at"), value.get("now"), unknown="Legacy/unknown"),
                    _remaining(account.get("cooldown_until"), value.get("now"), past="No", unknown="No"),
                    account.get("turns_last_hour") or 0,
                    account.get("turns_last_24_hours") or 0,
                    account.get("total_turns") or 0,
                    _time_text(account.get("uploaded_at")),
                    str(account.get("last_error") or "")[:180],
                ),
                tags=(tag,),
            )
            self.account_items[iid] = account
            if iid in selected:
                self.accounts_tree.selection_add(iid)

    def _update_operations(self, value: dict[str, Any]) -> None:
        self.commands_tree.delete(*self.commands_tree.get_children())
        self.command_items.clear()
        clients = {
            str(client.get("client_id")): client
            for client in value.get("clients", [])
            if isinstance(client, dict)
        }
        commands = value.get("commands") if isinstance(value.get("commands"), list) else []
        for command in commands:
            if not isinstance(command, dict):
                continue
            command_id = str(command.get("command_id") or "")
            client = clients.get(str(command.get("client_id") or ""), {})
            names = client.get("account_usernames") if isinstance(client.get("account_usernames"), list) else []
            payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
            result = command.get("result") if isinstance(command.get("result"), dict) else {}
            status = str(command.get("status") or "")
            tag = "online" if status == "completed" else ("failed" if status in {"failed", "expired"} else "warning")
            iid = "command-" + command_id
            self.commands_tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    _time_text(command.get("created_at")),
                    ", ".join(str(name) for name in names) or str(command.get("client_id") or "")[:12],
                    str(command.get("command") or "").replace("_", " ").title(),
                    str(payload.get("interaction") or "—").replace("_", " "),
                    status.title(),
                    command.get("attempts") or 0,
                    _result_summary(result),
                ),
                tags=(tag,),
            )
            self.command_items[iid] = command

        self.tests_tree.delete(*self.tests_tree.get_children())
        tests = value.get("copilot_tests") if isinstance(value.get("copilot_tests"), list) else []
        for test in tests:
            if not isinstance(test, dict):
                continue
            progress = test.get("progress") if isinstance(test.get("progress"), dict) else {}
            results = test.get("results") if isinstance(test.get("results"), list) else []
            result_text = ", ".join(
                f"{str(row.get('username') or row.get('account_id'))}: {'OK' if row.get('ok') else 'FAIL'}"
                for row in results
                if isinstance(row, dict)
            )
            stage = str(test.get("stage") or "")
            tag = "online" if stage == "complete" else ("failed" if stage == "failed" else "warning")
            self.tests_tree.insert(
                "",
                "end",
                iid="test-" + str(test.get("job_id") or time.time_ns()),
                values=(
                    _time_text(test.get("started_at")),
                    stage.replace("_", " ").title(),
                    f"{progress.get('completed', 0)}/{progress.get('total', 0)}",
                    test.get("message") or "",
                    result_text,
                ),
                tags=(tag,),
            )

    def _update_server(self, value: dict[str, Any]) -> None:
        server = value.get("server") if isinstance(value.get("server"), dict) else {}
        memory = server.get("memory") if isinstance(server.get("memory"), dict) else {}
        disk = server.get("disk") if isinstance(server.get("disk"), dict) else {}
        services = server.get("services") if isinstance(server.get("services"), dict) else {}
        lines = [
            f"Host:                 {server.get('host', 'unknown')}",
            f"API process uptime:   {_duration(server.get('api_uptime_seconds'))}",
            f"AWS system uptime:    {_duration(server.get('system_uptime_seconds'))}",
            f"Load average:         {server.get('load_average') or 'unknown'}",
            f"Memory available:     {_bytes(memory.get('available'))} / {_bytes(memory.get('total'))}",
            f"Disk free:            {_bytes(disk.get('free'))} / {_bytes(disk.get('total'))}",
            "",
            "Services",
            "--------",
        ]
        lines.extend(f"{name:<32} {state}" for name, state in services.items())
        lines.extend(
            [
                "",
                "Security",
                "--------",
                "Admin API uses a dedicated HMAC credential that is not included in the colleague client.",
                "Remote commands expire, are audited, and can only target clients seen online recently.",
                "Visible Microsoft authentication is only allowed when explicitly selected per command.",
            ]
        )
        self.server_text.configure(state="normal")
        self.server_text.delete("1.0", "end")
        self.server_text.insert("1.0", "\n".join(lines))
        self.server_text.configure(state="disabled")

    def _selected_clients(self) -> list[dict[str, Any]]:
        return [self.client_items[iid] for iid in self.clients_tree.selection() if iid in self.client_items]

    def force_renew(self) -> None:
        clients = self._selected_clients()
        if not clients:
            messagebox.showinfo("Select clients", "Select at least one online desktop client.")
            return
        offline = [client for client in clients if not client.get("online")]
        if offline:
            messagebox.showwarning("Offline selection", "Remove offline clients before forcing renewal.")
            return
        allow_visible = self.interaction_mode.get().startswith("Allow")
        if allow_visible and not messagebox.askyesno(
            "Allow visible Microsoft sign-in?",
            "This command may open Microsoft Edge on the selected colleagues’ computers and may request MFA. Continue?",
        ):
            return
        self._run_action(
            lambda: create_commands(
                self.config,
                client_ids=[str(client["client_id"]) for client in clients],
                command="force_renew",
                payload={"interaction": "allow_visible" if allow_visible else "silent_only"},
                expires_in_seconds=15 * 60,
            ),
            "Renewal command queued",
        )

    def force_update(self) -> None:
        clients = self._selected_clients()
        if not clients:
            messagebox.showinfo("Select clients", "Select at least one online desktop client.")
            return
        if any(not client.get("online") for client in clients):
            messagebox.showwarning("Offline selection", "Remove offline clients before forcing an update check.")
            return
        if not messagebox.askyesno(
            "Force update check",
            "Ask the selected clients to install the current approved GitHub release if their version differs?",
        ):
            return
        self._run_action(
            lambda: create_commands(
                self.config,
                client_ids=[str(client["client_id"]) for client in clients],
                command="force_update",
                payload={},
                expires_in_seconds=30 * 60,
            ),
            "Update command queued",
        )

    def forget_selected_clients(self) -> None:
        clients = self._selected_clients()
        if not clients:
            messagebox.showinfo("Select clients", "Select at least one offline unmapped installation.")
            return
        if any(client.get("online") for client in clients):
            messagebox.showwarning(
                "Online selection",
                "Only offline installations can be forgotten. Remove online clients from the selection.",
            )
            return
        if any(client.get("account_ids") for client in clients):
            messagebox.showwarning(
                "Mapped selection",
                "Only installations with no mapped Microsoft accounts can be forgotten.",
            )
            return
        short_ids = ", ".join(str(client.get("client_id") or "")[:8] for client in clients)
        if not messagebox.askyesno(
            "Forget selected installations?",
            "Remove the selected abandoned installation records from the admin view?\n\n"
            f"Clients: {short_ids}\n\n"
            "This does not uninstall anything and does not affect Copilot accounts.",
        ):
            return
        self._run_action(
            lambda: forget_clients(
                self.config,
                [str(client["client_id"]) for client in clients],
            ),
            "Selected installation records forgotten",
        )

    def ping_selected(self) -> None:
        account_ids = [
            str(self.account_items[iid]["account_id"])
            for iid in self.accounts_tree.selection()
            if iid in self.account_items
        ]
        if not account_ids:
            messagebox.showinfo("Select accounts", "Select at least one Copilot account to ping.")
            return
        self._start_ping(account_ids)

    def ping_all(self) -> None:
        account_ids = [str(account["account_id"]) for account in self.account_items.values()]
        if not account_ids:
            messagebox.showinfo("No accounts", "No Copilot accounts are registered.")
            return
        if not messagebox.askyesno(
            "Ping all Copilot accounts",
            f"Send one minimal prompt through each of the {len(account_ids)} accounts? This consumes one turn per account.",
        ):
            return
        self._start_ping(account_ids)

    def _start_ping(self, account_ids: list[str]) -> None:
        self._run_action(
            lambda: start_copilot_test(self.config, account_ids),
            "Copilot health test started",
            select_operations=True,
        )

    def cancel_selected_command(self) -> None:
        selected = self.commands_tree.selection()
        if not selected:
            messagebox.showinfo("Select command", "Select a queued or dispatched command.")
            return
        command = self.command_items.get(selected[0])
        if not command or command.get("status") not in {"queued", "dispatched"}:
            messagebox.showinfo("Cannot cancel", "Only queued or dispatched commands can be cancelled.")
            return
        self._run_action(
            lambda: cancel_command(self.config, str(command["command_id"])),
            "Command cancelled",
            select_operations=True,
        )

    def copy_client_diagnostics(self) -> None:
        clients = self._selected_clients()
        if not clients:
            messagebox.showinfo("Select clients", "Select at least one client.")
            return
        text = json.dumps(clients, ensure_ascii=False, indent=2)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status_text.set("Diagnostics copied")

    def _run_action(
        self,
        action: Callable[[], dict[str, Any]],
        success_message: str,
        *,
        select_operations: bool = False,
    ) -> None:
        self.status_text.set("Sending command…")

        def work() -> None:
            try:
                result = action()
            except BaseException as exc:
                self.root.after(
                    0,
                    lambda error=exc: messagebox.showerror("Administrator action failed", str(error)),
                )
                self.root.after(0, lambda: self.status_text.set("Action failed"))
                return

            def done() -> None:
                self.status_text.set(success_message)
                if select_operations:
                    self.notebook.select(self.operations_tab)
                rejected = result.get("rejected") if isinstance(result.get("rejected"), list) else []
                if rejected:
                    messagebox.showwarning("Some clients were rejected", json.dumps(rejected, indent=2))
                self.refresh()

            self.root.after(0, done)

        threading.Thread(target=work, name="token-admin-action", daemon=True).start()

    def _auto_refresh_tick(self) -> None:
        if self.closed:
            return
        if self.auto_refresh.get():
            self.refresh()
        self.root.after(10_000, self._auto_refresh_tick)

    def close(self) -> None:
        self.closed = True
        self.root.destroy()


def _time_text(value: object) -> str:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return "—"
    if timestamp <= 0:
        return "—"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def _remaining(value: object, now: object, *, past: str = "Expired", unknown: str = "Unknown") -> str:
    try:
        seconds = float(value) - float(now or time.time())
    except (TypeError, ValueError):
        return unknown
    return _duration(seconds) if seconds > 0 else past


def _duration(value: object) -> str:
    try:
        seconds = max(0, int(float(value)))
    except (TypeError, ValueError):
        return "unknown"
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, _ = divmod(seconds, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _bytes(value: object) -> str:
    try:
        size = float(value)
    except (TypeError, ValueError):
        return "unknown"
    for suffix in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or suffix == "TB":
            return f"{size:.1f} {suffix}"
        size /= 1024
    return "unknown"


def _result_summary(result: dict[str, Any]) -> str:
    if not result:
        return ""
    if result.get("error"):
        return str(result["error"])[:260]
    parts = [f"{key}={value}" for key, value in result.items() if value not in (None, "")]
    return ", ".join(parts)[:300]


def run() -> int:
    try:
        config = load_config()
    except Exception as exc:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("FMBSM Token Pool Admin", str(exc))
        root.destroy()
        return 1
    root = tk.Tk()
    TokenPoolAdminApp(root, config)
    root.mainloop()
    return 0
