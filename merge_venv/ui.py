from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Iterable

from .merger import EnvironmentMerger, MergeCancelled, MergeError
from .models import Conflict, EnvironmentSnapshot
from .scanner import (
    ScanError,
    build_conflicts,
    build_project_conflict_rows,
    find_environment,
    make_plan,
    scan_folders,
)


BG = "#f4f6f8"
PANEL = "#ffffff"
INK = "#17212b"
MUTED = "#637083"
ACCENT = "#2563eb"
ACCENT_DARK = "#1d4ed8"
LINE = "#dce2ea"
PROJECT_REPORT_HEADERS = ("Project folder", "Conflicting dependency", "Detected version")


def project_conflict_rows_as_tsv(rows: Iterable[tuple[str, str, str]]) -> str:
    """Format conflict rows for table-aware clipboard consumers."""
    table = [PROJECT_REPORT_HEADERS, *rows]
    return "\r\n".join(
        "\t".join(
            str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")
            for value in row
        )
        for row in table
    ) + "\r\n"


class MergeVenvApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Merge Venv")
        self.geometry("820x590")
        self.minsize(700, 500)
        self.configure(bg=BG)
        self._folders: list[Path] = []
        self._snapshots: tuple[EnvironmentSnapshot, ...] = ()
        self._conflicts: tuple[Conflict, ...] = ()
        self._selections: dict[str, str] = {}
        self._destination_parent: str | None = None
        self._destination_name = "merged-venv"
        self._worker_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._merger: EnvironmentMerger | None = None
        self._build_style()
        self._show_folder_screen()

    def _build_style(self) -> None:
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=INK, font=("Segoe UI", 10))
        style.configure("Panel.TLabel", background=PANEL, foreground=INK, font=("Segoe UI", 10))
        style.configure("Title.TLabel", background=BG, foreground=INK, font=("Segoe UI Semibold", 25))
        style.configure("Subtitle.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 11))
        style.configure("Section.TLabel", background=PANEL, foreground=INK, font=("Segoe UI Semibold", 12))
        style.configure("Muted.TLabel", background=PANEL, foreground=MUTED, font=("Segoe UI", 9))
        style.configure(
            "Accent.TButton",
            background=ACCENT,
            foreground="white",
            borderwidth=0,
            padding=(18, 10),
            font=("Segoe UI Semibold", 10),
        )
        style.map("Accent.TButton", background=[("active", ACCENT_DARK), ("disabled", "#9bb5e8")])
        style.configure("TButton", padding=(12, 8), font=("Segoe UI", 10))
        style.configure("Treeview", rowheight=38, font=("Segoe UI", 10), background=PANEL, fieldbackground=PANEL)
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 9), padding=(8, 7))

    def _clear(self) -> None:
        for child in self.winfo_children():
            child.destroy()

    def _page_header(self, title: str, subtitle: str) -> None:
        header = ttk.Frame(self, padding=(36, 30, 36, 18))
        header.pack(fill="x")
        ttk.Label(header, text=title, style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, text=subtitle, style="Subtitle.TLabel").pack(anchor="w", pady=(6, 0))

    def _show_folder_screen(self) -> None:
        self._clear()
        self._page_header(
            "Merge virtual environments",
            "Combine installed packages from several projects into one clean environment.",
        )
        panel = ttk.Frame(self, style="Panel.TFrame", padding=24)
        panel.pack(fill="both", expand=True, padx=36, pady=(0, 22))

        top = ttk.Frame(panel, style="Panel.TFrame")
        top.pack(fill="x")
        ttk.Label(top, text="Source folders", style="Section.TLabel").pack(side="left")
        ttk.Button(top, text="+ Add folders", command=self._add_folders).pack(side="right")

        ttk.Label(
            panel,
            text="Choose each top-level project folder. Its virtual environment is detected automatically.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(5, 14))

        columns = ("folder", "environment")
        self.folder_tree = ttk.Treeview(panel, columns=columns, show="headings", selectmode="extended")
        self.folder_tree.heading("folder", text="Selected folder")
        self.folder_tree.heading("environment", text="Environment")
        self.folder_tree.column("folder", width=360, anchor="w")
        self.folder_tree.column("environment", width=230, anchor="w")
        self.folder_tree.pack(fill="both", expand=True)

        empty = ttk.Label(
            self.folder_tree,
            text="No folders added yet",
            background=PANEL,
            foreground=MUTED,
            font=("Segoe UI", 11),
        )
        self._empty_label = empty
        empty.place(relx=.5, rely=.48, anchor="center")

        footer = ttk.Frame(panel, style="Panel.TFrame")
        footer.pack(fill="x", pady=(16, 0))
        ttk.Button(footer, text="Remove selected", command=self._remove_selected).pack(side="left")
        self.merge_button = ttk.Button(
            footer, text="Scan & merge  →", style="Accent.TButton", command=self._start_scan
        )
        self.merge_button.pack(side="right")
        self._refresh_folders()

    def _add_folders(self) -> None:
        # Tk's native picker selects one directory at a time; the list persists so
        # the button can be used repeatedly without typing paths.
        folder = filedialog.askdirectory(title="Choose a project folder containing a virtual environment")
        if folder:
            path = Path(folder).resolve()
            try:
                find_environment(path)
            except ScanError as exc:
                messagebox.showerror("Invalid project folder", str(exc), parent=self)
                return
            if path not in self._folders:
                self._folders.append(path)
                self._refresh_folders()

    def _remove_selected(self) -> None:
        selected = {int(item) for item in self.folder_tree.selection()}
        self._folders = [path for index, path in enumerate(self._folders) if index not in selected]
        self._refresh_folders()

    def _refresh_folders(self) -> None:
        for item in self.folder_tree.get_children():
            self.folder_tree.delete(item)
        for index, folder in enumerate(self._folders):
            try:
                environment = str(find_environment(folder))
            except ScanError:
                environment = "No usable environment found"
            self.folder_tree.insert("", "end", iid=str(index), values=(str(folder), environment))
        if self._folders:
            self._empty_label.place_forget()
        else:
            self._empty_label.place(relx=.5, rely=.48, anchor="center")
        self.merge_button.configure(state="normal" if self._folders else "disabled")

    def _start_scan(self) -> None:
        self._show_work_screen("Scanning environments", "Reading Python and installed package metadata…")
        threading.Thread(target=self._scan_worker, daemon=True).start()
        self.after(80, self._poll_scan)

    def _scan_worker(self) -> None:
        try:
            snapshots = scan_folders(self._folders)
            self._worker_queue.put(("done", snapshots))
        except Exception as exc:
            self._worker_queue.put(("error", exc))

    def _poll_scan(self) -> None:
        try:
            kind, payload = self._worker_queue.get_nowait()
        except queue.Empty:
            self.after(80, self._poll_scan)
            return
        if kind == "error":
            messagebox.showerror("Could not scan environments", str(payload), parent=self)
            self._show_folder_screen()
            return
        self._snapshots = payload  # type: ignore[assignment]
        self._conflicts = build_conflicts(self._snapshots)
        available_choices = {
            conflict.key: {choice.value for choice in conflict.choices}
            for conflict in self._conflicts
        }
        self._selections = {
            key: value
            for key, value in self._selections.items()
            if value in available_choices.get(key, set())
        }
        self._show_conflict_screen()

    def _show_work_screen(self, title: str, subtitle: str) -> None:
        self._clear()
        body = ttk.Frame(self, padding=50)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=title, style="Title.TLabel").pack(pady=(95, 8))
        ttk.Label(body, text=subtitle, style="Subtitle.TLabel").pack()
        progress = ttk.Progressbar(body, mode="indeterminate", length=360)
        progress.pack(pady=32)
        progress.start(10)

    def _show_conflict_screen(self) -> None:
        self._clear()
        conflict_count = len(self._conflicts)
        self._page_header(
            "Conflict report",
            (
                "Review each discrepancy and choose the version to keep. "
                "The newest detected version is selected by default."
                if conflict_count
                else "The selected environments use compatible Python and package versions."
            ),
        )
        panel = ttk.Frame(self, style="Panel.TFrame", padding=24)
        panel.pack(fill="both", expand=True, padx=36, pady=(0, 22))
        report_heading = (
            f"{conflict_count} version conflict{'s' if conflict_count != 1 else ''} require review"
            if conflict_count
            else "No version conflicts detected"
        )
        ttk.Label(panel, text=report_heading, style="Section.TLabel").pack(anchor="w")
        if not conflict_count:
            ttk.Label(
                panel,
                text="Continue to choose where the merged environment should be created.",
                style="Muted.TLabel",
            ).pack(anchor="w", pady=(10, 0))

        if conflict_count:
            notebook = ttk.Notebook(panel)
            notebook.pack(fill="both", expand=True, pady=(14, 0))
            dependency_view = ttk.Frame(notebook, style="Panel.TFrame", padding=(10, 4))
            project_view = ttk.Frame(notebook, style="Panel.TFrame", padding=(10, 12))
            notebook.add(dependency_view, text="Resolve by dependency")
            notebook.add(project_view, text="View by project")
        else:
            dependency_view = panel

        canvas = tk.Canvas(dependency_view, bg=PANEL, highlightthickness=0)
        scrollbar = ttk.Scrollbar(dependency_view, orient="vertical", command=canvas.yview)
        rows = ttk.Frame(canvas, style="Panel.TFrame")
        rows.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        window = canvas.create_window((0, 0), window=rows, anchor="nw")
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True, pady=(10, 0))
        scrollbar.pack(side="right", fill="y", pady=(10, 0))

        self._choice_vars: dict[str, tk.StringVar] = {}
        for index, conflict in enumerate(self._conflicts):
            row = ttk.Frame(rows, style="Panel.TFrame", padding=(4, 12))
            row.pack(fill="x")
            ttk.Label(row, text=conflict.display_name, style="Panel.TLabel", width=28).pack(side="left")
            choice_panel = ttk.Frame(row, style="Panel.TFrame")
            choice_panel.pack(side="right", fill="x", expand=True)
            values = [choice.value for choice in conflict.choices]
            selected = self._selections.get(conflict.key)
            variable = tk.StringVar(value=selected if selected in values else values[0])
            self._choice_vars[conflict.key] = variable
            self._selections[conflict.key] = variable.get()
            source_label = ttk.Label(
                choice_panel,
                text="",
                style="Muted.TLabel",
                justify="left",
                wraplength=390,
            )

            def update_choice(
                *_args,
                key=conflict.key,
                choice=variable,
                choices=conflict.choices,
                label=source_label,
            ) -> None:
                selected_value = choice.get()
                self._selections[key] = selected_value
                sources = next(
                    (item.sources for item in choices if item.value == selected_value), ()
                )
                label.configure(text="Found in: " + ", ".join(sources))

            variable.trace_add("write", update_choice)
            combo = ttk.Combobox(
                choice_panel, textvariable=variable, values=values, state="readonly", width=28
            )
            combo.pack(anchor="w")
            source_label.pack(anchor="w", pady=(5, 0))
            update_choice()
            if index < len(self._conflicts) - 1:
                ttk.Separator(rows).pack(fill="x")

        if conflict_count:
            columns = ("project", "dependency", "version")
            project_toolbar = ttk.Frame(project_view, style="Panel.TFrame")
            project_toolbar.pack(fill="x", pady=(0, 10))
            ttk.Label(
                project_toolbar,
                text="All projects and detected versions",
                style="Section.TLabel",
            ).pack(side="left")
            self._copy_table_button = ttk.Button(
                project_toolbar,
                text="Copy table",
                command=self._copy_project_conflict_table,
            )
            self._copy_table_button.pack(side="right")
            project_table = ttk.Frame(project_view, style="Panel.TFrame")
            project_table.pack(fill="both", expand=True)
            project_tree = ttk.Treeview(
                project_table, columns=columns, show="headings", selectmode="none"
            )
            project_tree.heading("project", text=PROJECT_REPORT_HEADERS[0])
            project_tree.heading("dependency", text=PROJECT_REPORT_HEADERS[1])
            project_tree.heading("version", text=PROJECT_REPORT_HEADERS[2])
            project_tree.column("project", width=350, minwidth=180, anchor="w")
            project_tree.column("dependency", width=190, minwidth=130, anchor="w")
            project_tree.column("version", width=150, minwidth=100, anchor="w")
            project_scrollbar = ttk.Scrollbar(
                project_table, orient="vertical", command=project_tree.yview
            )
            project_tree.configure(yscrollcommand=project_scrollbar.set)
            project_tree.pack(side="left", fill="both", expand=True)
            project_scrollbar.pack(side="right", fill="y")
            for index, values in enumerate(
                build_project_conflict_rows(self._snapshots, self._conflicts)
            ):
                project_tree.insert("", "end", iid=f"project-conflict-{index}", values=values)

        footer = ttk.Frame(self, padding=(36, 0, 36, 28))
        footer.pack(fill="x")
        ttk.Button(footer, text="← Back", command=self._show_folder_screen).pack(side="left")
        ttk.Button(
            footer,
            text="Use selected versions  →" if conflict_count else "Continue  →",
            style="Accent.TButton",
            command=self._accept_conflicts,
        ).pack(side="right")

    def _accept_conflicts(self) -> None:
        self._selections = {key: variable.get() for key, variable in self._choice_vars.items()}
        self._show_destination_screen()

    def _copy_project_conflict_table(self) -> None:
        rows = build_project_conflict_rows(self._snapshots, self._conflicts)
        try:
            self.clipboard_clear()
            self.clipboard_append(project_conflict_rows_as_tsv(rows))
            self.update_idletasks()
        except tk.TclError as exc:
            messagebox.showerror("Could not copy table", str(exc), parent=self)
            return
        self._copy_table_button.configure(text="Copied!")
        button = self._copy_table_button
        self.after(
            1600,
            lambda: button.configure(text="Copy table") if button.winfo_exists() else None,
        )

    def _show_destination_screen(self) -> None:
        self._clear()
        plan = make_plan(self._snapshots, self._selections)
        self._page_header("Choose a destination", "The destination must be a new folder.")
        panel = ttk.Frame(self, style="Panel.TFrame", padding=28)
        panel.pack(fill="both", expand=True, padx=36, pady=(0, 22))

        summary = ttk.Frame(panel, style="Panel.TFrame")
        summary.pack(fill="x", pady=(0, 28))
        for label, value in (
            ("Sources", str(len(plan.snapshots))),
            ("Python", plan.selected_python),
            ("Packages", str(plan.package_count)),
        ):
            card = ttk.Frame(summary, style="Panel.TFrame", padding=(0, 8))
            card.pack(side="left", expand=True, fill="x")
            ttk.Label(card, text=value, style="Section.TLabel").pack()
            ttk.Label(card, text=label, style="Muted.TLabel").pack(pady=(3, 0))

        ttk.Label(panel, text="Parent folder", style="Section.TLabel").pack(anchor="w")
        parent_row = ttk.Frame(panel, style="Panel.TFrame")
        parent_row.pack(fill="x", pady=(8, 20))
        if self._destination_parent is None:
            self._destination_parent = str(self._folders[0].parent if self._folders else Path.home())
        self._parent_var = tk.StringVar(value=self._destination_parent)
        ttk.Entry(parent_row, textvariable=self._parent_var).pack(side="left", fill="x", expand=True)
        ttk.Button(parent_row, text="Browse…", command=self._choose_parent).pack(side="right", padx=(8, 0))

        ttk.Label(panel, text="Environment folder name", style="Section.TLabel").pack(anchor="w")
        self._name_var = tk.StringVar(value=self._destination_name)
        ttk.Entry(panel, textvariable=self._name_var).pack(fill="x", pady=(8, 8))
        self._preview_label = ttk.Label(panel, text="", style="Muted.TLabel")
        self._preview_label.pack(anchor="w")
        self._parent_var.trace_add("write", self._update_preview)
        self._name_var.trace_add("write", self._update_preview)
        self._parent_var.trace_add("write", self._remember_destination)
        self._name_var.trace_add("write", self._remember_destination)
        self._update_preview()

        footer = ttk.Frame(self, padding=(36, 0, 36, 28))
        footer.pack(fill="x")
        ttk.Button(footer, text="← Back", command=self._show_conflict_screen).pack(side="left")
        ttk.Button(
            footer, text="Create environment", style="Accent.TButton", command=self._start_merge
        ).pack(side="right")

    def _choose_parent(self) -> None:
        folder = filedialog.askdirectory(title="Choose the destination parent folder")
        if folder:
            self._parent_var.set(folder)

    def _update_preview(self, *_args) -> None:
        try:
            target = Path(self._parent_var.get()) / self._name_var.get().strip()
            self._preview_label.configure(text=f"Will create: {target}")
        except (TypeError, ValueError):
            self._preview_label.configure(text="")

    def _remember_destination(self, *_args) -> None:
        self._destination_parent = self._parent_var.get()
        self._destination_name = self._name_var.get()

    def _start_merge(self) -> None:
        self._remember_destination()
        name = self._name_var.get().strip()
        if not name or name in {".", ".."} or Path(name).name != name:
            messagebox.showerror("Invalid name", "Enter a single new folder name.", parent=self)
            return
        target = (Path(self._parent_var.get()).expanduser() / name).resolve()
        if not target.parent.is_dir():
            messagebox.showerror("Invalid destination", "The parent folder does not exist.", parent=self)
            return
        if target.exists():
            messagebox.showerror("Destination exists", "Choose a folder name that does not exist.", parent=self)
            return

        plan = make_plan(self._snapshots, self._selections)
        self._show_progress_screen(target)
        cancel_event = threading.Event()
        self._merger = EnvironmentMerger(
            log=lambda message: self._worker_queue.put(("log", message)),
            cancel_event=cancel_event,
        )
        threading.Thread(target=self._merge_worker, args=(plan, target), daemon=True).start()
        self.after(80, self._poll_merge)

    def _show_progress_screen(self, target: Path) -> None:
        self._clear()
        self._page_header("Creating merged environment", str(target))
        panel = ttk.Frame(self, style="Panel.TFrame", padding=22)
        panel.pack(fill="both", expand=True, padx=36, pady=(0, 18))
        self._progress = ttk.Progressbar(panel, mode="indeterminate")
        self._progress.pack(fill="x", pady=(0, 16))
        self._progress.start(10)
        self._log = tk.Text(
            panel,
            height=16,
            state="disabled",
            bg="#101820",
            fg="#d9e2ec",
            insertbackground="white",
            relief="flat",
            padx=14,
            pady=12,
            font=("Cascadia Mono", 9),
        )
        self._log.pack(fill="both", expand=True)
        footer = ttk.Frame(self, padding=(36, 0, 36, 24))
        footer.pack(fill="x")
        self._cancel_button = ttk.Button(footer, text="Cancel", command=self._cancel_merge)
        self._cancel_button.pack(side="right")

    def _append_log(self, message: str) -> None:
        self._log.configure(state="normal")
        self._log.insert("end", message + "\n")
        self._log.see("end")
        self._log.configure(state="disabled")

    def _merge_worker(self, plan, target: Path) -> None:
        try:
            assert self._merger is not None
            result = self._merger.merge(plan, target)
            self._worker_queue.put(("done", result))
        except Exception as exc:
            self._worker_queue.put(("error", exc))

    def _poll_merge(self) -> None:
        while True:
            try:
                kind, payload = self._worker_queue.get_nowait()
            except queue.Empty:
                self.after(80, self._poll_merge)
                return
            if kind == "log":
                self._append_log(str(payload))
                continue
            self._progress.stop()
            self._cancel_button.configure(state="disabled")
            if kind == "done":
                messagebox.showinfo(
                    "Merge complete", f"The merged environment is ready at:\n{payload}", parent=self
                )
                self._show_folder_screen()
            else:
                title = "Merge cancelled" if isinstance(payload, MergeCancelled) else "Merge failed"
                messagebox.showerror(title, str(payload), parent=self)
                self._show_destination_screen()
            return

    def _cancel_merge(self) -> None:
        if self._merger:
            self._cancel_button.configure(state="disabled")
            self._append_log("Cancelling…")
            self._merger.cancel()


def run() -> None:
    app = MergeVenvApp()
    app.mainloop()
