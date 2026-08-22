"""SISU book catalog filler — crawl a bookstore and write colored Excel columns."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from urllib.parse import urlparse

from dataclasses import asdict

from app_config import (
    BROWSERS,
    browser_executable,
    browser_label,
    load_config,
    merged_publisher_rows,
    normalize_site_url,
    save_config,
)
from book_crawler import Book, BookCrawler, CrawlCancelled, CrawlReport, format_price, parse_site_urls, site_display_name, site_host
from book_table import ROW_STATUSES, BookTable
from catalog_excel import CatalogWorkbook
from field_map import ALIASES_PATH, REPORT_JSON_PATH, reload_aliases, write_field_report
from hebrew_view import HebrewDescription
from publisher_sites import publishers_match, resolve_publisher_site
from scanner_registry import attach_book, attach_books, mark_excel_ids, persist_book_state
from scan_lists import (
    books_from_payload,
    build_payload,
    default_scan_title,
    delete_named,
    empty_payload,
    list_summaries,
    load_named,
    load_stash,
    load_working,
    rename_named,
    save_named,
    save_stash,
    save_working,
    set_named_flags,
    stash_exists,
    stash_summary,
)

APP_DIR = Path(__file__).resolve().parent
WATCHED_FILES = (
    "app.py",
    "app_update.py",
    "book_table.py",
    "book_crawler.py",
    "book_cache.py",
    "catalog_excel.py",
    "hebrew_view.py",
    "publisher_sites.py",
    "app_config.py",
    "field_map.py",
    "scanner_registry.py",
    "scan_lists.py",
)
RELOAD_SENTINEL = APP_DIR / "cache" / ".reload"
SCHEMA_EXCEL = APP_DIR / "master our program.xlsx"
DEFAULT_EXCEL = APP_DIR / "Data enter - bulk - MASTER our program.xlsx"
if not DEFAULT_EXCEL.exists() and SCHEMA_EXCEL.exists():
    DEFAULT_EXCEL = SCHEMA_EXCEL
NAVY = "#1F3651"
BG = "#F4F1EA"
WHITE = "#FFFFFF"
TAB_ON = "#C47B2B"
TAB_OFF = "#D9D0C3"
NEW_FG = "#146C43"
NEW_BG = "#E4F7EA"
DEFAULT_URLS = "\n".join(
    (
        "https://www.booknet.co.il/ספרים-חדשים",
        "https://www.e-vrit.co.il/",
        "https://www.nli.org.il/he/search?materialType=books",
    )
)


class BookCatalogApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("SISU Book Catalog Filler")
        self.geometry("1280x780")
        self.minsize(1020, 640)
        self.configure(bg=BG)

        self.excel_path = tk.StringVar(value=str(DEFAULT_EXCEL if DEFAULT_EXCEL.exists() else ""))
        self.year = tk.StringVar(value="2026")
        self.max_pages = tk.IntVar(value=5)
        self.include_unknown = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="Start a new list, or open a saved scan list.")
        self.summary = tk.StringVar(value="")
        self.colored_info = tk.StringVar(value="")
        self.excel_columns: list[dict] = []
        self.excel_all_columns: list[dict] = []

        self.books: list[Book] = []
        self.list_title = tk.StringVar(value="New")
        self.list_status = tk.StringVar(value="Working list · new")
        self._list_id = ""
        self._list_locked = False
        self._list_archived = False
        self._list_created_at = ""
        self._list_notes = ""
        self._list_report: dict = {}
        self._lists_popup: tk.Toplevel | None = None
        self._selected_book: Book | None = None
        self._description_text = ""
        self._copy_toast: tk.Toplevel | None = None
        self._copy_toast_after: str | None = None
        self._lookup_popup: tk.Toplevel | None = None
        self._lookup_status = tk.StringVar(value="")
        self._lookup_step = tk.StringVar(value="")
        self._lookup_title = tk.StringVar(value="")
        self._lookup_hint = tk.StringVar(value="")
        self._lookup_bar: ttk.Progressbar | None = None
        self._lookup_log: tk.Text | None = None
        self._lookup_stop_btn: ttk.Button | None = None
        self._lookup_close_btn: ttk.Button | None = None
        self._lookup_running = False
        self._found_popup: tk.Toplevel | None = None
        self._settings_popup: tk.Toplevel | None = None
        self._updating = False
        self._update_declined = False
        self._cancel = threading.Event()
        self._busy = False
        self._ui_queue: queue.Queue = queue.Queue()

        self._setup_style()
        self._build()
        self.refresh_excel_info()
        self._restore_working_list()
        self._mtimes = _source_mtimes()
        self.after(120, self._drain_queue)
        self.after(900, self._watch_for_reload)
        self.after(2000, lambda: self.check_for_updates(silent=True))

    def _setup_style(self) -> None:
        style = ttk.Style(self)
        if sys.platform == "win32":
            style.theme_use("vista")
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=WHITE)
        style.configure("TLabel", background=BG, font=("Segoe UI", 10))
        style.configure("Card.TLabel", background=WHITE, font=("Segoe UI", 10))
        style.configure("Header.TLabel", background=NAVY, foreground=WHITE, font=("Segoe UI", 16, "bold"))
        style.configure("Sub.TLabel", background=NAVY, foreground="#E8D5B5", font=("Segoe UI", 9))
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))

    def _build(self) -> None:
        header = tk.Frame(self, bg=NAVY)
        header.pack(fill="x")
        ttk.Label(header, text="SISU Book Catalog Filler", style="Header.TLabel").pack(
            side="left", padx=18, pady=(14, 2)
        )
        ttk.Label(
            header,
            text="Crawl the first site, fill gaps from the next URLs, then write orange Excel columns",
            style="Sub.TLabel",
        ).pack(side="left", padx=12, pady=(18, 8))
        ttk.Button(header, text="Check for updates", command=lambda: self.check_for_updates(silent=False)).pack(
            side="right", padx=(0, 8), pady=12
        )
        ttk.Button(header, text="Field report", command=self.open_field_report).pack(side="right", padx=(0, 8), pady=12)
        ttk.Button(header, text="Lists", command=self.open_lists_manager).pack(side="right", padx=(0, 8), pady=12)
        ttk.Button(header, text="Settings", command=self.open_settings).pack(side="right", padx=16, pady=12)

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=14, pady=12)

        lists = ttk.LabelFrame(body, text="Scan list")
        lists.pack(fill="x", pady=(0, 8))
        lists.columnconfigure(1, weight=1)
        ttk.Label(lists, text="Title").grid(row=0, column=0, sticky="e", padx=(10, 8), pady=6)
        self.list_title_entry = ttk.Entry(lists, textvariable=self.list_title)
        self.list_title_entry.grid(row=0, column=1, sticky="ew", pady=6)
        self.list_title_entry.bind("<FocusOut>", lambda _e: self._persist_working())
        list_btns = ttk.Frame(lists)
        list_btns.grid(row=0, column=2, padx=10, pady=6, sticky="e")
        ttk.Button(list_btns, text="New", command=self.new_working_list).pack(side="left", padx=(0, 4))
        ttk.Button(list_btns, text="Stash", command=self.stash_working_list).pack(side="left", padx=(0, 4))
        ttk.Button(list_btns, text="Restore stash", command=self.restore_stash).pack(side="left", padx=(0, 4))
        ttk.Button(list_btns, text="Save list", command=self.save_current_list).pack(side="left", padx=(0, 4))
        ttk.Button(list_btns, text="Open lists…", command=self.open_lists_manager).pack(side="left")
        ttk.Label(lists, textvariable=self.list_status, wraplength=1100).grid(
            row=1, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 8)
        )

        form = ttk.LabelFrame(body, text="Search")
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="Excel file").grid(row=0, column=0, sticky="e", padx=(10, 8), pady=6)
        ttk.Entry(form, textvariable=self.excel_path).grid(row=0, column=1, columnspan=3, sticky="ew", pady=6)
        excel_btns = ttk.Frame(form)
        excel_btns.grid(row=0, column=4, padx=10, pady=6, sticky="e")
        ttk.Button(excel_btns, text="Browse…", command=self.browse_excel).pack(side="left")
        ttk.Button(excel_btns, text="Open", command=self.open_excel).pack(side="left", padx=(6, 0))

        ttk.Label(form, text="Site URLs").grid(row=1, column=0, sticky="ne", padx=(10, 8), pady=6)
        url_wrap = ttk.Frame(form)
        url_wrap.grid(row=1, column=1, columnspan=4, sticky="ew", pady=6, padx=(0, 10))
        url_wrap.columnconfigure(0, weight=1)
        self.url_text = tk.Text(url_wrap, height=4, wrap="none", font=("Segoe UI", 10), undo=True)
        url_scroll = ttk.Scrollbar(url_wrap, orient="vertical", command=self.url_text.yview)
        self.url_text.configure(yscrollcommand=url_scroll.set)
        self.url_text.grid(row=0, column=0, sticky="ew")
        url_scroll.grid(row=0, column=1, sticky="ns")
        self.url_text.insert("1.0", DEFAULT_URLS)

        ttk.Label(form, text="Year").grid(row=2, column=0, sticky="e", padx=(10, 8), pady=6)
        self.year_entry = ttk.Entry(form, textvariable=self.year, width=10)
        self.year_entry.grid(row=2, column=1, sticky="w", pady=6)
        ttk.Label(form, text="Max listing pages").grid(row=2, column=2, sticky="e", padx=(16, 8))
        self.pages_spin = ttk.Spinbox(form, from_=1, to=40, textvariable=self.max_pages, width=6)
        self.pages_spin.grid(row=2, column=3, sticky="w")
        buttons = ttk.Frame(form)
        buttons.grid(row=2, column=4, sticky="e", padx=10, pady=6)
        self.search_btn = ttk.Button(buttons, text="Search", command=self.start_search, style="Accent.TButton")
        self.search_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(buttons, text="Stop", command=self.stop_search, state="disabled")
        self.stop_btn.pack(side="left", padx=4)

        checks = ttk.Frame(form)
        checks.grid(row=3, column=1, columnspan=4, sticky="w", pady=(0, 8))
        self.unknown_check = ttk.Checkbutton(
            checks, text="Also keep books with no year listed", variable=self.include_unknown
        )
        self.unknown_check.pack(side="left")

        ttk.Label(body, textvariable=self.colored_info, wraplength=1180).pack(anchor="w", pady=(8, 4))
        self.progress = ttk.Progressbar(body, mode="indeterminate")
        self.progress.pack(fill="x", pady=(0, 4))
        ttk.Label(body, textvariable=self.summary, wraplength=1180).pack(anchor="w", pady=(0, 8))

        split = ttk.Panedwindow(body, orient="horizontal")
        split.pack(fill="both", expand=True)
        list_frame = ttk.Frame(split)
        detail_frame = ttk.Frame(split, style="Card.TFrame")
        split.add(list_frame, weight=3)
        split.add(detail_frame, weight=2)

        self.table = BookTable(
            list_frame,
            on_select=self.show_book,
            on_check=self._on_check_change,
            on_publisher=self.ask_publisher_lookup,
            on_action=self.handle_book_action,
        )
        filter_bar = ttk.Frame(list_frame)
        filter_bar.pack(fill="x", pady=(0, 4))
        ttk.Label(filter_bar, text="Filter").pack(side="left")
        self._filter_vars: dict[str, tk.BooleanVar] = {}
        for key, label in ROW_STATUSES:
            var = tk.BooleanVar(value=False)
            self._filter_vars[key] = var
            ttk.Checkbutton(filter_bar, text=label, variable=var, command=self._on_filter_change).pack(
                side="left", padx=(8, 0)
            )
        ttk.Button(filter_bar, text="Clear books…", command=self.clear_books_with_keep).pack(side="right")
        self.table.pack(fill="both", expand=True)

        detail_header = ttk.Frame(detail_frame, style="Card.TFrame")
        detail_header.pack(fill="x", padx=12, pady=(10, 6))
        ttk.Label(detail_header, text="Selected book", style="Card.TLabel", font=("Segoe UI", 11, "bold")).pack(
            side="left"
        )
        self.final_btn = ttk.Button(detail_header, text="Mark final", command=self.mark_selected_final, state="disabled")
        self.final_btn.pack(side="right")
        self.approve_btn = ttk.Button(detail_header, text="Approve", command=self.approve_selected, state="disabled")
        self.approve_btn.pack(side="right", padx=(0, 6))
        self.more_btn = ttk.Button(detail_header, text="More", command=self.lookup_more, state="disabled")
        self.more_btn.pack(side="right", padx=(0, 6))

        detail_split = ttk.Panedwindow(detail_frame, orient="vertical")
        detail_split.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        info_pane = tk.Frame(detail_split, bg=WHITE)
        desc_pane = tk.Frame(detail_split, bg=WHITE)
        detail_split.add(info_pane, weight=3)
        detail_split.add(desc_pane, weight=2)

        scroll_wrap = tk.Frame(info_pane, bg=WHITE)
        scroll_wrap.pack(fill="both", expand=True)
        scroll_wrap.columnconfigure(0, weight=1)
        scroll_wrap.rowconfigure(0, weight=1)
        self.detail_canvas = tk.Canvas(scroll_wrap, bg=WHITE, highlightthickness=0)
        detail_scroll = ttk.Scrollbar(scroll_wrap, orient="vertical", command=self.detail_canvas.yview)
        self.detail_inner = tk.Frame(self.detail_canvas, bg=WHITE)
        self._detail_window_id = self.detail_canvas.create_window((0, 0), window=self.detail_inner, anchor="nw")
        self.detail_canvas.configure(yscrollcommand=detail_scroll.set)
        self.detail_canvas.grid(row=0, column=0, sticky="nsew")
        detail_scroll.grid(row=0, column=1, sticky="ns")
        self.detail_inner.columnconfigure(0, minsize=168)
        self.detail_inner.columnconfigure(1, minsize=20)
        self.detail_inner.columnconfigure(2, weight=1)
        self._value_labels: list[tk.Label] = []

        def _sync_detail_scroll(_event=None) -> None:
            self.detail_canvas.configure(scrollregion=self.detail_canvas.bbox("all"))
            width = self.detail_canvas.winfo_width()
            if width > 1:
                self.detail_canvas.itemconfigure(self._detail_window_id, width=width)
            wrap = max(140, width - 210)
            for label in self._value_labels:
                label.configure(wraplength=wrap)

        self.detail_inner.bind("<Configure>", _sync_detail_scroll)
        self.detail_canvas.bind("<Configure>", _sync_detail_scroll)

        def _on_detail_wheel(event: tk.Event) -> str | None:
            self.detail_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            return "break"

        self.detail_canvas.bind("<Enter>", lambda _e: self.detail_canvas.bind_all("<MouseWheel>", _on_detail_wheel))
        self.detail_canvas.bind("<Leave>", lambda _e: self.detail_canvas.unbind_all("<MouseWheel>"))
        self.detail_inner.bind("<Enter>", lambda _e: self.detail_canvas.bind_all("<MouseWheel>", _on_detail_wheel))

        desc_header = ttk.Frame(desc_pane, style="Card.TFrame")
        desc_header.pack(fill="x", padx=4, pady=(6, 2))
        ttk.Label(desc_header, text="Description", style="Card.TLabel", font=("Segoe UI", 10, "bold")).pack(
            side="left"
        )
        self.desc_new_label = tk.Label(
            desc_header,
            text="new from publisher",
            bg=NEW_BG,
            fg=NEW_FG,
            font=("Segoe UI", 8, "bold"),
            padx=6,
            pady=1,
        )
        self.copy_desc_btn = tk.Canvas(
            desc_header,
            width=22,
            height=22,
            bg=WHITE,
            highlightthickness=0,
            cursor="hand2",
        )
        self.copy_desc_btn.pack(side="right")
        self.copy_desc_btn.bind("<Button-1>", lambda _e: self.copy_description())
        self._draw_copy_icon(False)
        desc_wrap = tk.Frame(desc_pane, bg=WHITE)
        desc_wrap.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        self.desc_view = HebrewDescription(desc_wrap)
        self.desc_view.pack(fill="both", expand=True)

        footer = ttk.Frame(body)
        footer.pack(fill="x", pady=(8, 0))
        ttk.Button(footer, text="Select all", command=self.table.select_all).pack(side="left")
        ttk.Button(footer, text="Clear selection", command=self.table.clear_selection).pack(side="left", padx=6)
        ttk.Button(footer, text="Mark selected final", command=self.mark_selected_final_checked).pack(side="left")
        ttk.Button(
            footer,
            text="Approve selected",
            command=self.approve_checked,
            style="Accent.TButton",
        ).pack(side="right")
        ttk.Label(footer, textvariable=self.status).pack(side="left", padx=16)

    def _urls(self) -> list[str]:
        return parse_site_urls(self.url_text.get("1.0", "end"))

    def _current_publishers(self) -> list[str]:
        names: list[str] = []
        for book in self.books:
            name = (book.publisher or "").strip()
            if name and not any(publishers_match(name, existing) for existing in names):
                names.append(name)
        return names

    def open_settings(self, focus_publisher: str = "", focus_tab: str = "") -> None:
        existing = getattr(self, "_settings_popup", None)
        if existing is not None:
            if focus_publisher or focus_tab:
                try:
                    existing.destroy()
                except tk.TclError:
                    pass
                self._settings_popup = None
            else:
                try:
                    existing.lift()
                    existing.focus_force()
                    return
                except tk.TclError:
                    self._settings_popup = None
        data = load_config()
        win = tk.Toplevel(self)
        win.title("Settings")
        win.configure(bg=BG)
        win.transient(self)
        win.geometry("820x600")
        win.minsize(680, 500)
        self._settings_popup = win

        def close() -> None:
            try:
                win.unbind_all("<MouseWheel>")
            except tk.TclError:
                pass
            self._settings_popup = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", close)
        body = ttk.Frame(win, padding=14)
        body.pack(fill="both", expand=True)

        tab_bar = tk.Frame(body, bg=BG)
        tab_bar.pack(fill="x", pady=(0, 8))
        browser_tab = ttk.Frame(body, padding=12)
        publisher_tab = ttk.Frame(body, padding=12)
        aliases_tab = ttk.Frame(body, padding=12)
        tab_frames = {
            "browser": browser_tab,
            "publishers": publisher_tab,
            "aliases": aliases_tab,
        }
        tab_buttons: dict[str, tk.Button] = {}

        def show_tab(name: str) -> None:
            for frame in tab_frames.values():
                frame.pack_forget()
            tab_frames[name].pack(fill="both", expand=True)
            for key, button in tab_buttons.items():
                if key == name:
                    button.configure(bg=TAB_ON, fg=WHITE, activebackground="#A86620", activeforeground=WHITE)
                else:
                    button.configure(bg=TAB_OFF, fg=NAVY, activebackground="#C9BBA8", activeforeground=NAVY)

        def make_tab(key: str, label: str) -> None:
            button = tk.Button(
                tab_bar,
                text=label,
                font=("Segoe UI", 10, "bold"),
                relief="flat",
                bd=0,
                padx=16,
                pady=8,
                cursor="hand2",
                command=lambda tab=key: show_tab(tab),
            )
            button.pack(side="left", padx=(0, 6))
            tab_buttons[key] = button

        make_tab("browser", "Browser")
        make_tab("publishers", "Publisher sites")
        make_tab("aliases", "Field aliases")

        browser_var = tk.StringVar(value=data.get("browser") or "chrome")
        custom_path = tk.StringVar(value=data.get("browser_path") or "")
        ttk.Label(
            browser_tab,
            text="When you click a book or publisher link, open it in:",
            wraplength=680,
        ).pack(anchor="w")
        choices = ttk.Frame(browser_tab)
        choices.pack(anchor="w", pady=(8, 10))
        for key, label in BROWSERS:
            ttk.Radiobutton(choices, text=label, value=key, variable=browser_var).pack(anchor="w", pady=2)
        custom_row = ttk.Frame(browser_tab)
        custom_row.pack(fill="x", pady=(4, 0))
        ttk.Label(custom_row, text="Custom executable").pack(side="left")
        ttk.Entry(custom_row, textvariable=custom_path).pack(side="left", fill="x", expand=True, padx=8)

        def browse_browser() -> None:
            path = filedialog.askopenfilename(
                title="Select a browser executable",
                filetypes=[("Programs", "*.exe"), ("All files", "*.*")],
            )
            if path:
                custom_path.set(path)
                browser_var.set("custom")

        ttk.Button(custom_row, text="Browse…", command=browse_browser).pack(side="left")
        ttk.Label(
            browser_tab,
            text="Google Chrome is the default. Choose another browser, or the system default, if you prefer.",
            wraplength=680,
        ).pack(anchor="w", pady=(12, 0))

        ttk.Label(
            publisher_tab,
            text="Known publisher websites are filled in. Empty rows are publishers from the current list — add their site and Save. Open opens the site in your browser.",
            wraplength=740,
        ).pack(anchor="w")
        table_wrap = ttk.Frame(publisher_tab)
        table_wrap.pack(fill="both", expand=True, pady=(8, 6))
        canvas = tk.Canvas(table_wrap, bg=BG, highlightthickness=0)
        scroll = ttk.Scrollbar(table_wrap, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.columnconfigure(1, weight=1)
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        rows: list[tuple[tk.StringVar, tk.StringVar]] = []

        def _sync_scroll(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(window_id, width=canvas.winfo_width())

        inner.bind("<Configure>", _sync_scroll)
        canvas.bind("<Configure>", _sync_scroll)

        def _on_mousewheel(event: tk.Event) -> None:
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _on_mousewheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        def _paint_url(entry: tk.Entry, var: tk.StringVar) -> None:
            entry.configure(bg="#FFF4CC" if not var.get().strip() else WHITE)

        def _append_data_row(name: str = "", url: str = "", highlight: bool = False) -> None:
            name_var = tk.StringVar(value=name)
            url_var = tk.StringVar(value=url)
            grid_row = len(rows) + 1
            name_entry = tk.Entry(inner, textvariable=name_var, font=("Segoe UI", 10), relief="solid", bd=1, width=28)
            url_entry = tk.Entry(inner, textvariable=url_var, font=("Segoe UI", 10), relief="solid", bd=1)
            name_entry.grid(row=grid_row, column=0, sticky="ew", padx=(0, 6), pady=3, ipady=3)
            url_entry.grid(row=grid_row, column=1, sticky="ew", padx=(0, 6), pady=3, ipady=3)
            _paint_url(url_entry, url_var)
            url_var.trace_add("write", lambda *_args: _paint_url(url_entry, url_var))
            if highlight:
                url_entry.focus_set()
            open_btn = tk.Button(
                inner,
                text="Open",
                font=("Segoe UI", 9, "underline"),
                fg="#0B57D0",
                activeforeground="#0B57D0",
                relief="flat",
                cursor="hand2",
                command=lambda item=url_var: self._open_publisher_site(item.get()),
            )
            open_btn.grid(row=grid_row, column=2, padx=(0, 4), pady=3)

            def _sync_open(*_args: object, button=open_btn, var=url_var) -> None:
                has_url = bool(var.get().strip())
                button.configure(
                    state="normal" if has_url else "disabled",
                    fg="#0B57D0" if has_url else "#9A9A9A",
                    cursor="hand2" if has_url else "arrow",
                )

            url_var.trace_add("write", _sync_open)
            _sync_open()
            pair = (name_var, url_var)
            ttk.Button(inner, text="Remove", command=lambda item=pair: _remove_and_rebuild(item)).grid(
                row=grid_row, column=3, pady=3
            )
            rows.append(pair)

        def _remove_and_rebuild(pair: tuple[tk.StringVar, tk.StringVar]) -> None:
            snapshot = [(name.get(), url.get()) for name, url in rows if (name, url) != pair]
            _fill_rows(snapshot)

        def _fill_rows(items: list[tuple[str, str]], highlight_name: str = "") -> None:
            for child in inner.winfo_children():
                child.destroy()
            rows.clear()
            ttk.Label(inner, text="Publisher", font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky="w")
            ttk.Label(inner, text="Website", font=("Segoe UI", 9, "bold")).grid(row=0, column=1, sticky="w")
            ttk.Label(inner, text="").grid(row=0, column=2)
            for name, url in items:
                _append_data_row(
                    name,
                    url,
                    highlight=bool(highlight_name) and publishers_match(name, highlight_name),
                )

        seed = merged_publisher_rows(self._current_publishers())
        if focus_publisher.strip() and not any(publishers_match(focus_publisher, name) for name, _url in seed):
            seed.insert(0, (focus_publisher.strip(), ""))
        _fill_rows(seed, highlight_name=focus_publisher)

        ttk.Button(
            publisher_tab,
            text="Add publisher",
            command=lambda: _append_data_row("", "", highlight=True),
        ).pack(anchor="w")

        ttk.Label(
            aliases_tab,
            text="Conversion table used at runtime. Add a page label (Hebrew or English) and the catalog field it should fill, then Save.",
            wraplength=740,
        ).pack(anchor="w")
        ttk.Label(
            aliases_tab,
            text=str(ALIASES_PATH),
            font=("Segoe UI", 8),
            wraplength=740,
        ).pack(anchor="w", pady=(2, 6))
        alias_wrap = ttk.Frame(aliases_tab)
        alias_wrap.pack(fill="both", expand=True)
        alias_wrap.columnconfigure(0, weight=1)
        alias_wrap.rowconfigure(0, weight=1)
        alias_text = tk.Text(alias_wrap, wrap="none", font=("Consolas", 10), undo=True)
        alias_y = ttk.Scrollbar(alias_wrap, orient="vertical", command=alias_text.yview)
        alias_x = ttk.Scrollbar(alias_wrap, orient="horizontal", command=alias_text.xview)
        alias_text.configure(yscrollcommand=alias_y.set, xscrollcommand=alias_x.set)
        alias_text.grid(row=0, column=0, sticky="nsew")
        alias_y.grid(row=0, column=1, sticky="ns")
        alias_x.grid(row=1, column=0, sticky="ew")
        if ALIASES_PATH.exists():
            alias_text.insert("1.0", ALIASES_PATH.read_text(encoding="utf-8"))
        else:
            alias_text.insert("1.0", '{\n  "comment": "",\n  "aliases": {\n  }\n}\n')

        def save_aliases() -> bool:
            raw = alias_text.get("1.0", "end").strip()
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                messagebox.showerror("Field aliases", f"The JSON is not valid:\n{exc}")
                return False
            if not isinstance(parsed, dict) or not isinstance(parsed.get("aliases"), dict):
                messagebox.showerror("Field aliases", 'The file must be a JSON object with an "aliases" map.')
                return False
            ALIASES_PATH.write_text(json.dumps(parsed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            reload_aliases()
            self._set_status("Field aliases saved. The next Search or More will use them.")
            return True

        alias_btns = ttk.Frame(aliases_tab)
        alias_btns.pack(fill="x", pady=(8, 0))
        ttk.Button(alias_btns, text="Save aliases", command=save_aliases, style="Accent.TButton").pack(side="left")
        ttk.Button(alias_btns, text="Open file…", command=lambda: self._open_local_file(ALIASES_PATH)).pack(
            side="left", padx=8
        )

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(10, 0))

        def save() -> None:
            publishers: dict[str, str] = {}
            for name_var, url_var in rows:
                name = name_var.get().strip()
                if not name:
                    continue
                publishers[name] = normalize_site_url(url_var.get())
            save_config(
                {
                    "browser": browser_var.get().strip() or "chrome",
                    "browser_path": custom_path.get().strip(),
                    "publishers": publishers,
                }
            )
            if not save_aliases():
                show_tab("aliases")
                return
            close()
            self._set_status("Settings saved.")
            if self._selected_book:
                self.show_book(self._selected_book)

        ttk.Button(buttons, text="Cancel", command=close).pack(side="right")
        ttk.Button(buttons, text="Save", command=save, style="Accent.TButton").pack(side="right", padx=(0, 8))
        start_tab = "aliases" if focus_tab == "aliases" else "publishers" if focus_publisher.strip() else "browser"
        show_tab(start_tab)
        win.lift()
        win.focus_force()

    def browse_excel(self) -> None:
        path = filedialog.askopenfilename(
            title="Select catalog Excel file",
            filetypes=[("Excel files", "*.xlsx *.xlsm"), ("All files", "*.*")],
            initialdir=str(APP_DIR),
        )
        if path:
            self.excel_path.set(path)
            self.refresh_excel_info()

    def open_excel(self) -> None:
        path = Path(self.excel_path.get().strip())
        if not path.exists():
            messagebox.showerror("Excel", "The Excel file was not found.")
            return
        try:
            os.startfile(str(path))
            self._set_status(f"Opened {path.name}")
        except OSError as exc:
            messagebox.showerror("Could not open Excel", str(exc))

    def _open_local_file(self, path: Path) -> None:
        if not path.exists():
            messagebox.showerror("File", f"The file was not found:\n{path}")
            return
        try:
            os.startfile(str(path))
            self._set_status(f"Opened {path.name}")
        except OSError as exc:
            messagebox.showerror("Could not open file", str(exc))

    def _open_publisher_site(self, url: str) -> None:
        site = normalize_site_url(url)
        if not site:
            messagebox.showinfo("Publisher site", "This row has no website yet.")
            return
        self._open_in_browser(site)

    def refresh_excel_info(self) -> None:
        path = Path(self.excel_path.get().strip())
        if not path.exists():
            self.colored_info.set("No Excel file selected yet.")
            return
        try:
            catalog = CatalogWorkbook(path)
        except Exception as exc:
            self.colored_info.set(f"Could not read Excel headers: {exc}")
            self.excel_columns = []
            self.excel_all_columns = []
            return
        self.excel_columns = catalog.columns
        self.excel_all_columns = catalog.all_columns
        names = ", ".join(catalog.colored_headers) or "(none found)"
        unmapped = [col["header"] for col in catalog.columns if not col.get("field")]
        extra = f" Unmapped orange headers: {', '.join(unmapped)}." if unmapped else ""
        self.colored_info.set(f"Orange/colored columns that will be filled: {names}.{extra}")
        mark_excel_ids(catalog.existing_scanner_ids())

    def _current_payload(self, report: dict | None = None) -> dict:
        return build_payload(
            books=self.books,
            urls=self._urls(),
            year=self.year.get().strip(),
            title=self.list_title.get().strip() or "New",
            list_id=self._list_id,
            locked=self._list_locked,
            archived=self._list_archived,
            max_pages=int(self.max_pages.get() or 5),
            include_unknown=bool(self.include_unknown.get()),
            report=report if report is not None else self._list_report,
            notes=self._list_notes,
            created_at=self._list_created_at,
        )

    def _persist_working(self, report: dict | None = None) -> None:
        if report is not None:
            self._list_report = report
        save_working(self._current_payload())
        self._refresh_list_status()

    def _refresh_list_status(self) -> None:
        if self._list_locked:
            kind = "Locked list"
        elif self._list_id:
            kind = "Saved list"
        elif self.books:
            kind = "Working list · unsaved"
        else:
            kind = "Working list · new"
        extra = []
        if self._list_archived:
            extra.append("archived")
        if stash_exists():
            extra.append("stash ready")
        suffix = f" · {', '.join(extra)}" if extra else ""
        self.list_status.set(f"{kind} · {len(self.books)} book(s){suffix}")
        self._apply_lock_state()

    def _apply_lock_state(self) -> None:
        locked = self._list_locked and not self._busy
        search_state = "disabled" if (self._list_locked or self._busy) else "normal"
        edit_state = "disabled" if self._list_locked else "normal"
        self.search_btn.configure(state=search_state)
        try:
            self.url_text.configure(state=edit_state)
        except tk.TclError:
            pass
        self.year_entry.configure(state=edit_state)
        self.pages_spin.configure(state=edit_state)
        self.unknown_check.configure(state=edit_state)
        self.list_title_entry.configure(state=edit_state)
        if self._list_locked:
            self.more_btn.configure(state="disabled")
            self.approve_btn.configure(state="disabled")
            self.final_btn.configure(state="disabled")
        elif self._selected_book and not self._busy:
            self.more_btn.configure(state="normal")
            self._update_workflow_buttons(self._selected_book)

    def _apply_payload(self, data: dict, *, status: str = "") -> None:
        self._list_id = str(data.get("id") or "")
        self._list_locked = bool(data.get("locked"))
        self._list_archived = bool(data.get("archived"))
        self._list_created_at = str(data.get("created_at") or "")
        self._list_notes = str(data.get("notes") or "")
        self._list_report = data.get("report") or {}
        self.list_title.set(str(data.get("title") or "New"))
        urls = [str(url) for url in (data.get("urls") or []) if str(url).strip()]
        self.url_text.configure(state="normal")
        self.url_text.delete("1.0", "end")
        self.url_text.insert("1.0", "\n".join(urls) if urls else DEFAULT_URLS)
        if data.get("year") not in (None, ""):
            self.year.set(str(data.get("year")))
        if data.get("max_pages"):
            try:
                self.max_pages.set(int(data.get("max_pages")))
            except (TypeError, ValueError, tk.TclError):
                pass
        if "include_unknown" in data:
            self.include_unknown.set(bool(data.get("include_unknown")))
        self.books = books_from_payload(data)
        self._prepare_books(self.books)
        self.table.set_books(self.books, keep_checks=False)
        self._selected_book = None
        self._persist_working()
        report = self._list_report
        if report:
            try:
                summary = CrawlReport(**{k: report[k] for k in CrawlReport.__dataclass_fields__ if k in report})
                summary.matched = len(self.books)
                self.summary.set(summary.summary())
            except TypeError:
                self.summary.set(f"{len(self.books)} book(s) in this list.")
        else:
            self.summary.set(f"{len(self.books)} book(s) in this list.")
        self._set_status(status or f"Opened “{self.list_title.get()}” with {len(self.books)} book(s).")
        self._refresh_list_status()

    def _restore_working_list(self) -> None:
        data = load_working()
        if not data:
            self._refresh_list_status()
            return
        self._apply_payload(
            data,
            status=f"Restored the last working list “{data.get('title') or 'New'}” with {len(data.get('books') or [])} book(s).",
        )

    def new_working_list(self) -> None:
        if self._busy:
            return
        if self.books and not messagebox.askyesno(
            "New list",
            "Clear the current working list? Stash it first if you want to keep these books.",
        ):
            return
        urls = self._urls() or parse_site_urls(DEFAULT_URLS)
        payload = empty_payload(
            title="New",
            urls=urls,
            year=self.year.get().strip(),
            max_pages=int(self.max_pages.get() or 5),
            include_unknown=bool(self.include_unknown.get()),
        )
        self._apply_payload(payload, status="Started a new empty working list.")

    def stash_working_list(self) -> None:
        if self._busy:
            return
        if stash_exists() and not messagebox.askyesno(
            "Stash",
            f"Replace the existing stash?\n\n{stash_summary()}",
        ):
            return
        save_stash(self._current_payload())
        urls = self._urls() or parse_site_urls(DEFAULT_URLS)
        payload = empty_payload(
            title="New",
            urls=urls,
            year=self.year.get().strip(),
            max_pages=int(self.max_pages.get() or 5),
            include_unknown=bool(self.include_unknown.get()),
        )
        self._apply_payload(payload, status="Stashed the previous list. This working list is empty for a new search.")

    def restore_stash(self) -> None:
        if self._busy:
            return
        data = load_stash()
        if not data:
            messagebox.showinfo("Stash", "There is no stashed list to restore.")
            return
        if self.books and not messagebox.askyesno(
            "Restore stash",
            f"Replace the current working list with the stash?\n\n{stash_summary()}",
        ):
            return
        self._apply_payload(data, status=f"Restored stash: {stash_summary()}.")

    def save_current_list(self) -> None:
        if self._list_locked:
            messagebox.showinfo("Locked", "This list is locked. Unlock it before saving changes.")
            return
        title = self.list_title.get().strip()
        if not title or title == "New":
            title = default_scan_title(len(self.books), self.year.get().strip())
            entered = simpledialog.askstring(
                "Save list",
                "Name for this scan list:",
                initialvalue=title,
                parent=self,
            )
            if entered is None:
                return
            title = entered.strip() or title
            self.list_title.set(title)
        payload = save_named(self._current_payload())
        self._list_id = str(payload.get("id") or "")
        self._list_created_at = str(payload.get("created_at") or "")
        self._persist_working()
        self._set_status(f"Saved list “{title}”.")
        if self._lists_popup is not None:
            self._reload_lists_table()

    def _guard_locked(self, action: str = "change this list") -> bool:
        if not self._list_locked:
            return False
        messagebox.showinfo("Locked list", f"This list is locked, so you cannot {action}. Unlock it in Lists first.")
        return True

    def open_lists_manager(self) -> None:
        existing = getattr(self, "_lists_popup", None)
        if existing is not None:
            try:
                existing.lift()
                existing.focus_force()
                return
            except tk.TclError:
                self._lists_popup = None
        win = tk.Toplevel(self)
        win.title("Scan lists")
        win.configure(bg=BG)
        win.transient(self)
        win.geometry("860x480")
        win.minsize(720, 380)
        self._lists_popup = win
        win.protocol("WM_DELETE_WINDOW", lambda: self._close_lists_popup())
        body = ttk.Frame(win, padding=14)
        body.pack(fill="both", expand=True)
        ttk.Label(
            body,
            text="Saved scan lists. Open one to make it the working list. Locked lists can be viewed but not searched or changed.",
            wraplength=800,
        ).pack(anchor="w")
        self._show_archived = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            body,
            text="Show archived lists",
            variable=self._show_archived,
            command=self._reload_lists_table,
        ).pack(anchor="w", pady=(8, 6))
        wrap = ttk.Frame(body)
        wrap.pack(fill="both", expand=True)
        columns = ("title", "books", "year", "updated", "state")
        tree = ttk.Treeview(wrap, columns=columns, show="headings", selectmode="browse", height=12)
        tree.heading("title", text="Title")
        tree.heading("books", text="Books")
        tree.heading("year", text="Year")
        tree.heading("updated", text="Updated")
        tree.heading("state", text="State")
        tree.column("title", width=280, anchor="w")
        tree.column("books", width=70, anchor="center")
        tree.column("year", width=70, anchor="center")
        tree.column("updated", width=160, anchor="center")
        tree.column("state", width=140, anchor="w")
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self._lists_tree = tree
        tree.bind("<Double-1>", lambda _e: self._open_selected_saved_list())
        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="Open", command=self._open_selected_saved_list).pack(side="left")
        ttk.Button(btns, text="Rename", command=self._rename_selected_list).pack(side="left", padx=4)
        ttk.Button(btns, text="Lock", command=lambda: self._flag_selected_list(locked=True)).pack(side="left")
        ttk.Button(btns, text="Unlock", command=lambda: self._flag_selected_list(locked=False)).pack(side="left", padx=4)
        ttk.Button(btns, text="Archive", command=lambda: self._flag_selected_list(archived=True)).pack(side="left")
        ttk.Button(btns, text="Unarchive", command=lambda: self._flag_selected_list(archived=False)).pack(
            side="left", padx=4
        )
        ttk.Button(btns, text="Delete…", command=self._delete_selected_list).pack(side="left")
        ttk.Button(btns, text="Close", command=self._close_lists_popup).pack(side="right")
        self._reload_lists_table()

    def _close_lists_popup(self) -> None:
        win = self._lists_popup
        self._lists_popup = None
        if win is not None:
            try:
                win.destroy()
            except tk.TclError:
                pass

    def _reload_lists_table(self) -> None:
        tree = getattr(self, "_lists_tree", None)
        if tree is None:
            return
        tree.delete(*tree.get_children())
        for item in list_summaries(include_archived=bool(self._show_archived.get())):
            state = []
            if item.get("locked"):
                state.append("locked")
            if item.get("archived"):
                state.append("archived")
            updated = str(item.get("updated_at") or "")[:16].replace("T", " ")
            tree.insert(
                "",
                "end",
                iid=str(item.get("id") or ""),
                values=(
                    item.get("title") or "Untitled",
                    item.get("book_count") or 0,
                    item.get("year") or "",
                    updated,
                    ", ".join(state) or "saved",
                ),
            )

    def _selected_list_id(self) -> str:
        tree = getattr(self, "_lists_tree", None)
        if tree is None:
            return ""
        selection = tree.selection()
        return str(selection[0]) if selection else ""

    def _open_selected_saved_list(self) -> None:
        list_id = self._selected_list_id()
        if not list_id:
            return
        data = load_named(list_id)
        if not data:
            messagebox.showerror("Lists", "That list file could not be opened.")
            return
        if self.books and not messagebox.askyesno(
            "Open list",
            "Replace the current working list with this saved list?\nStash first if you want to keep the unsaved books.",
        ):
            return
        self._apply_payload(data)
        self._close_lists_popup()

    def _rename_selected_list(self) -> None:
        list_id = self._selected_list_id()
        if not list_id:
            return
        current = load_named(list_id) or {}
        if current.get("locked"):
            messagebox.showinfo("Locked", "Unlock the list before renaming it.")
            return
        entered = simpledialog.askstring(
            "Rename list",
            "New title:",
            initialvalue=str(current.get("title") or ""),
            parent=self._lists_popup or self,
        )
        if entered is None or not entered.strip():
            return
        rename_named(list_id, entered.strip())
        if self._list_id == list_id:
            self.list_title.set(entered.strip())
            self._persist_working()
        self._reload_lists_table()

    def _flag_selected_list(self, locked: bool | None = None, archived: bool | None = None) -> None:
        list_id = self._selected_list_id()
        if not list_id:
            return
        payload = set_named_flags(list_id, locked=locked, archived=archived)
        if not payload:
            return
        if self._list_id == list_id:
            if locked is not None:
                self._list_locked = bool(locked)
            if archived is not None:
                self._list_archived = bool(archived)
            self._persist_working()
        self._reload_lists_table()

    def _delete_selected_list(self) -> None:
        list_id = self._selected_list_id()
        if not list_id:
            return
        current = load_named(list_id) or {}
        title = str(current.get("title") or "this list")
        if current.get("locked"):
            messagebox.showinfo("Locked", "Unlock the list before deleting it.")
            return
        if not messagebox.askyesno(
            "Delete list",
            f"Delete the saved list “{title}”?\n\nThis does not delete the books from Excel. It only removes this scan list.",
        ):
            return
        if not messagebox.askyesno(
            "Delete list",
            "This cannot be undone. Delete the list permanently?",
        ):
            return
        typed = simpledialog.askstring(
            "Confirm delete",
            f'Type DELETE to permanently remove “{title}”:',
            parent=self._lists_popup or self,
        )
        if (typed or "").strip().upper() != "DELETE":
            messagebox.showinfo("Delete cancelled", "The list was not deleted.")
            return
        delete_named(list_id)
        if self._list_id == list_id:
            self._list_id = ""
            self._list_locked = False
            self._list_archived = False
            self._persist_working()
        self._reload_lists_table()
        self._set_status(f"Deleted list “{title}”.")

    def start_search(self) -> None:
        if self._busy:
            return
        if self._guard_locked("search again"):
            return
        urls = self._urls()
        year = self.year.get().strip()
        if not urls:
            messagebox.showwarning("Missing URL", "Paste one bookstore URL per line.")
            return
        if year and not year.isdigit():
            messagebox.showwarning("Year", "Publication year should be a number such as 2026.")
            return

        self._cancel.clear()
        self._busy = True
        self.search_btn.configure(state="disabled")
        self.more_btn.configure(state="disabled")
        self.approve_btn.configure(state="disabled")
        self.final_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.progress.start(12)
        self._set_status("Starting crawl…")
        thread = threading.Thread(
            target=self._run_crawl,
            args=(
                urls,
                year,
                int(self.max_pages.get() or 5),
                bool(self.include_unknown.get()),
                self.list_title.get().strip(),
                self._list_id,
                self._list_locked,
                self._list_archived,
                self._list_notes,
                self._list_created_at,
            ),
            daemon=True,
        )
        thread.start()

    def stop_search(self) -> None:
        self._cancel.set()
        self._set_status("Stopping…")

    def _run_crawl(
        self,
        urls: list[str],
        year: str,
        max_pages: int,
        include_unknown: bool,
        title: str,
        list_id: str,
        locked: bool,
        archived: bool,
        notes: str,
        created_at: str,
    ) -> None:
        crawler = BookCrawler(
            cancelled=self._cancel.is_set,
            progress=lambda msg: self._ui_queue.put(("status", msg)),
        )
        try:
            self._ui_queue.put(("status", f"Searching first site: {urls[0]}"))
            books = crawler.crawl(
                start_url=urls[0],
                year=year,
                max_listing_pages=max_pages,
                include_unknown_year=include_unknown,
            )
            if urls[1:] and books:
                crawler.enrich_books(books, urls[1:])
            self._prepare_books(books)
            save_working(
                build_payload(
                    books=books,
                    urls=urls,
                    year=year,
                    title=title or default_scan_title(len(books), year),
                    list_id=list_id,
                    locked=locked,
                    archived=archived,
                    max_pages=max_pages,
                    include_unknown=include_unknown,
                    report=asdict(crawler.report),
                    notes=notes,
                    created_at=created_at,
                )
            )
            try:
                write_field_report(self.excel_path.get().strip() or None)
            except Exception:
                pass
            self._ui_queue.put(("done", (books, crawler.report)))
        except CrawlCancelled:
            self._ui_queue.put(("cancelled", None))
        except Exception as exc:
            self._ui_queue.put(("error", str(exc)))

    def _drain_queue(self) -> None:
        while True:
            try:
                kind, payload = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "status":
                self.status.set(str(payload))
                if self._lookup_popup is not None:
                    self._lookup_status.set(str(payload))
                    self._append_lookup_log(str(payload))
            elif kind == "lookup_step":
                self._set_lookup_step(payload)
            elif kind == "done":
                books, report = payload
                self._finish_search(books, cancelled=False, report=report)
            elif kind == "cancelled":
                self._finish_search(self.books, cancelled=True)
            elif kind == "error":
                self._finish_search([], cancelled=False)
                messagebox.showerror("Search failed", str(payload))
            elif kind == "more_done":
                self._finish_more(payload)
            elif kind == "more_error":
                self._finish_more(None)
                messagebox.showerror("Could not look up more details", str(payload))
            elif kind == "update_result":
                silent, info = payload
                self._on_update_result(bool(silent), info)
            elif kind == "update_error":
                silent, text = payload
                self._on_update_error(bool(silent), str(text))
            elif kind == "update_applied":
                self._finish_self_update()
        self.after(120, self._drain_queue)

    def _finish_search(self, books: list[Book], cancelled: bool, report: CrawlReport | None = None) -> None:
        self._busy = False
        self.stop_btn.configure(state="disabled")
        self.progress.stop()
        if books:
            self.books = books
            self._prepare_books(self.books)
            self.table.set_books(self.books, keep_checks=False)
        if (not self.list_title.get().strip() or self.list_title.get().strip() == "New") and self.books:
            self.list_title.set(default_scan_title(len(self.books), self.year.get().strip()))
        if report is None:
            report = CrawlReport(matched=len(self.books), cancelled=cancelled)
        self._list_report = asdict(report)
        self._persist_working(self._list_report)
        year = self.year.get().strip() or "any year"
        prefix = "Stopped. " if cancelled else ""
        self.status.set(
            f"{prefix}{len(self.books)} book(s) from {year}. The working list is kept until you save or clear it."
        )
        self.summary.set(report.summary())
        if self._selected_book:
            self.more_btn.configure(state="normal")
            self._update_workflow_buttons(self._selected_book)
        self._refresh_list_status()

    def _on_check_change(self) -> None:
        self._set_status(f"{len(self.table.checked)} selected")

    def show_book(self, book: Book) -> None:
        self._selected_book = book
        if not self._busy:
            self.more_btn.configure(state="normal")
        self._update_workflow_buttons(book)
        fields = book.to_excel_fields()
        self._set_details(book, fields)
        self._set_links(_book_link_items(book))
        description = fields["description_he"] or book.description or ""
        self._set_description(description)
        new_fields = {part for part in (book.extra.get("new_fields") or "").split(",") if part}
        if "description" in new_fields:
            self.desc_new_label.configure(text="new")
            self.desc_new_label.pack(side="left", padx=(8, 0))
        else:
            self.desc_new_label.pack_forget()

    def lookup_more(self) -> None:
        if self._guard_locked("look up more details"):
            return
        book = self._selected_book
        if not book or self._busy:
            return
        if not book.publisher.strip():
            messagebox.showinfo("Publisher", "This book has no publisher, so More cannot open a publisher site.")
            return
        site = resolve_publisher_site(book.publisher)
        if not site:
            if messagebox.askyesno(
                "Publisher site missing",
                f"No publisher website is saved for:\n\n{book.publisher}\n\n"
                "Open Settings to add the site?",
            ):
                self.open_settings(focus_publisher=book.publisher)
            return
        self._start_publisher_lookup([book], selected=book)

    def ask_publisher_lookup(self, book: Book) -> None:
        if self._guard_locked("look up publisher details"):
            return
        if self._busy:
            return
        name = (book.publisher or "").strip()
        if not name:
            messagebox.showinfo("Publisher", "This book has no publisher.")
            return
        peers = [item for item in self.books if publishers_match(item.publisher, name)]
        needing = [item for item in peers if item.missing_fields()]
        site = resolve_publisher_site(name)
        if not needing:
            messagebox.showinfo(
                "Publisher",
                f"All {len(peers)} book(s) from {name} already have the fillable fields.",
            )
            return
        if not site:
            if messagebox.askyesno(
                "Publisher site missing",
                f"No publisher website is saved for:\n\n{name}\n\n"
                "Open Settings to add the site?",
            ):
                self.open_settings(focus_publisher=name)
            return
        host = urlparse(site).netloc
        if not messagebox.askyesno(
            "Look up publisher details",
            f"Search {host} for missing details on all books from:\n\n{name}\n\n"
            f"{len(needing)} of {len(peers)} book(s) still have empty fields.\n\nContinue?",
        ):
            return
        self._start_publisher_lookup(needing, selected=book)

    def _start_publisher_lookup(self, books: list[Book], selected: Book | None = None) -> None:
        if not books or self._busy:
            return
        self._cancel.clear()
        self._busy = True
        self.search_btn.configure(state="disabled")
        self.more_btn.configure(state="disabled")
        self.approve_btn.configure(state="disabled")
        self.final_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.progress.start(12)
        label = books[0].publisher.strip() if books else "publisher"
        site = resolve_publisher_site(label) or ""
        self._set_status(f"Looking up missing details on the publisher site for {len(books)} book(s)…")
        self._open_lookup_popup(publisher=label, site=site, total=len(books))
        thread = threading.Thread(
            target=self._run_publisher_lookup,
            args=(books, selected, label),
            daemon=True,
        )
        thread.start()

    def _open_lookup_popup(self, publisher: str, site: str, total: int) -> None:
        self._close_lookup_popup()
        self._lookup_running = True
        win = tk.Toplevel(self)
        win.title("Looking up publisher details")
        win.configure(bg=BG)
        win.transient(self)
        win.resizable(True, True)
        win.protocol("WM_DELETE_WINDOW", self._on_lookup_close_request)
        body = ttk.Frame(win, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, textvariable=self._lookup_title, font=("Segoe UI", 12, "bold")).pack(anchor="w")
        host = urlparse(site).netloc if site else publisher
        ttk.Label(body, text=host).pack(anchor="w", pady=(2, 8))
        ttk.Label(body, textvariable=self._lookup_step, wraplength=560).pack(anchor="w")
        ttk.Label(body, textvariable=self._lookup_status, wraplength=560).pack(anchor="w", pady=(4, 8))
        bar = ttk.Progressbar(body, length=560)
        bar.pack(fill="x")
        if total > 1:
            bar.configure(mode="determinate", maximum=total, value=0)
        else:
            bar.configure(mode="indeterminate")
            bar.start(12)
        self._lookup_bar = bar
        ttk.Label(body, text="Crawl log", font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(12, 4))
        log = tk.Text(
            body,
            height=12,
            wrap="word",
            font=("Segoe UI", 10),
            relief="flat",
            bd=0,
            highlightthickness=0,
            bg=BG,
            fg="#1B1B1B",
            padx=0,
            pady=0,
        )
        log.pack(fill="both", expand=True)
        log.bind("<Key>", lambda _e: "break")
        self._lookup_log = log
        ttk.Label(
            body,
            textvariable=self._lookup_hint,
            wraplength=560,
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w", pady=(10, 8))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x")
        self._lookup_stop_btn = ttk.Button(buttons, text="Stop", command=self.stop_search)
        self._lookup_stop_btn.pack(side="right")
        self._lookup_close_btn = ttk.Button(buttons, text="Close", command=self._close_lookup_popup)
        self._lookup_title.set("Crawling the publisher site…")
        self._lookup_step.set(f"Book 1 of {total}" if total else "Starting…")
        self._lookup_status.set("Opening the publisher catalog…")
        self._lookup_hint.set("This window stays open so you can read the crawl results.")
        self.update_idletasks()
        width, height = 580, 520
        x = self.winfo_rootx() + max(0, (self.winfo_width() - width) // 2)
        y = self.winfo_rooty() + max(0, (self.winfo_height() - height) // 2)
        win.geometry(f"{width}x{height}+{x}+{y}")
        win.minsize(520, 420)
        win.lift()
        self._lookup_popup = win

    def _append_lookup_log(self, message: str) -> None:
        widget = self._lookup_log
        if widget is None:
            return
        widget.insert("end", message.rstrip() + "\n")
        widget.see("end")

    def _set_lookup_step(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        index = int(payload.get("index") or 0)
        total = int(payload.get("total") or 0)
        title = str(payload.get("title") or "")
        if total:
            self._lookup_step.set(f"Book {index} of {total}\n{title}")
        if self._lookup_bar is not None and str(self._lookup_bar.cget("mode")) == "determinate":
            self._lookup_bar["value"] = index

    def _on_lookup_close_request(self) -> None:
        if self._lookup_running:
            self.stop_search()
            return
        self._close_lookup_popup()

    def _complete_lookup_popup(self, summary: str) -> None:
        self._lookup_running = False
        if self._lookup_bar is not None:
            try:
                self._lookup_bar.stop()
            except tk.TclError:
                pass
            if str(self._lookup_bar.cget("mode")) == "determinate":
                try:
                    self._lookup_bar["value"] = self._lookup_bar.cget("maximum")
                except tk.TclError:
                    pass
        self._lookup_title.set("Lookup finished")
        self._lookup_status.set(summary)
        self._lookup_hint.set("Read the crawl log above, then click Close.")
        self._append_lookup_log("")
        self._append_lookup_log(summary)
        if self._lookup_stop_btn is not None:
            self._lookup_stop_btn.pack_forget()
        if self._lookup_close_btn is not None:
            self._lookup_close_btn.pack(side="right")
        if self._lookup_popup is not None:
            try:
                self._lookup_popup.lift()
            except tk.TclError:
                pass

    def _show_found_fields_popup(self, book: Book) -> None:
        findings = book.publisher_found_fields()
        if not findings:
            return
        self._close_found_fields_popup()
        win = tk.Toplevel(self)
        win.title("Fields found on the book page")
        win.configure(bg=BG)
        win.transient(self)
        win.resizable(True, True)
        win.protocol("WM_DELETE_WINDOW", self._close_found_fields_popup)
        body = ttk.Frame(win, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Fields found on the book page", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        ttk.Label(body, text=book.display_title(), wraplength=560).pack(anchor="w", pady=(2, 10))
        wrap = ttk.Frame(body)
        wrap.pack(fill="both", expand=True)
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        canvas = tk.Canvas(wrap, bg=WHITE, highlightthickness=0)
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=WHITE)
        inner.columnconfigure(0, weight=0)
        inner.columnconfigure(1, weight=1)
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        def _sync(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(window_id, width=canvas.winfo_width())

        inner.bind("<Configure>", _sync)
        canvas.bind("<Configure>", _sync)

        def _on_mousewheel(event: tk.Event) -> None:
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _on_mousewheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        for row, item in enumerate(findings):
            tk.Label(
                inner,
                text=item["label"],
                bg=WHITE,
                fg="#5A6570",
                font=("Segoe UI", 10),
                anchor="e",
                justify="right",
                wraplength=200,
            ).grid(row=row, column=0, sticky="ne", padx=(12, 10), pady=6)
            tk.Label(
                inner,
                text=item["value"],
                bg=NEW_BG,
                fg=NEW_FG,
                font=("Segoe UI", 10),
                anchor="w",
                justify="left",
                wraplength=340,
                padx=10,
                pady=6,
            ).grid(row=row, column=1, sticky="ew", padx=(0, 12), pady=4)

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="Close", command=self._close_found_fields_popup).pack(side="right")
        self.update_idletasks()
        width, height = 620, min(640, 180 + 36 * max(len(findings), 4))
        x = self.winfo_rootx() + max(0, (self.winfo_width() - width) // 2)
        y = self.winfo_rooty() + max(0, (self.winfo_height() - height) // 2)
        win.geometry(f"{width}x{height}+{x}+{y}")
        win.minsize(480, 280)
        win.lift()
        win.focus_force()
        self._found_popup = win

    def _close_found_fields_popup(self) -> None:
        win = self._found_popup
        self._found_popup = None
        if win is not None:
            try:
                win.destroy()
            except tk.TclError:
                pass

    def _close_lookup_popup(self) -> None:
        self._lookup_running = False
        if self._lookup_bar is not None:
            try:
                self._lookup_bar.stop()
            except tk.TclError:
                pass
            self._lookup_bar = None
        self._lookup_log = None
        self._lookup_stop_btn = None
        self._lookup_close_btn = None
        win = self._lookup_popup
        self._lookup_popup = None
        if win is not None:
            try:
                win.destroy()
            except tk.TclError:
                pass

    def _run_publisher_lookup(self, books: list[Book], selected: Book | None, publisher: str) -> None:
        crawler = BookCrawler(
            cancelled=self._cancel.is_set,
            progress=lambda msg: self._ui_queue.put(("status", msg)),
        )
        remap: dict[str, str] = {}
        updated = 0
        try:
            for index, book in enumerate(books, start=1):
                self._ui_queue.put(
                    ("lookup_step", {"index": index, "total": len(books), "title": book.display_title()})
                )
                crawler.progress(f"{index}/{len(books)}  {book.display_title()}")
                old_key = book.key()
                filled = crawler.enrich_one_book(book)
                if old_key != book.key():
                    remap[old_key] = book.key()
                if filled:
                    updated += 1
            save_working(self._current_payload())
            try:
                write_field_report(self.excel_path.get().strip() or None)
            except Exception:
                pass
            self._ui_queue.put(
                (
                    "more_done",
                    {
                        "updated": updated,
                        "total": len(books),
                        "cancelled": False,
                        "remap": remap,
                        "selected": selected,
                        "publisher": publisher,
                    },
                )
            )
        except CrawlCancelled:
            save_working(self._current_payload())
            self._ui_queue.put(
                (
                    "more_done",
                    {
                        "updated": updated,
                        "total": len(books),
                        "cancelled": True,
                        "remap": remap,
                        "selected": selected,
                        "publisher": publisher,
                    },
                )
            )
        except Exception as exc:
            self._ui_queue.put(("more_error", str(exc)))

    def open_field_report(self) -> None:
        try:
            report = write_field_report(self.excel_path.get().strip() or None)
        except Exception as exc:
            messagebox.showerror("Field report", str(exc))
            return
        self._set_status(f"Field matching report saved: {report}")
        win = tk.Toplevel(self)
        win.title("Field matching files")
        win.configure(bg=BG)
        win.transient(self)
        win.geometry("560x280")
        body = ttk.Frame(win, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(
            body,
            text="The markdown report lists page labels found on book sites. The JSON conversion table is what the program uses to match those labels to catalog fields — you can view and edit it here.",
            wraplength=520,
        ).pack(anchor="w")

        def row(label: str, path: Path, extra=None) -> None:
            bar = ttk.Frame(body)
            bar.pack(fill="x", pady=(10, 0))
            ttk.Label(bar, text=label).pack(side="left")
            ttk.Button(bar, text="Open", command=lambda: self._open_local_file(path)).pack(side="right")
            if extra:
                extra(bar)

        row("Field report (markdown)", report)
        row("Field report (JSON)", REPORT_JSON_PATH)

        def alias_buttons(bar: ttk.Frame) -> None:
            ttk.Button(
                bar,
                text="Edit in Settings",
                command=lambda: (win.destroy(), self.open_settings(focus_tab="aliases")),
            ).pack(side="right", padx=(0, 8))

        row("Field aliases (JSON conversion table)", ALIASES_PATH, extra=alias_buttons)
        ttk.Button(body, text="Close", command=win.destroy).pack(side="right", pady=(18, 0))
        win.lift()
        win.focus_force()

    def _finish_more(self, payload: dict | None) -> None:
        self._busy = False
        self.search_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.progress.stop()
        remap = (payload or {}).get("remap") or None
        selected = (payload or {}).get("selected") or self._selected_book
        self._prepare_books(self.books)
        self._persist_working()
        self.table.set_books(self.books, keep_checks=True, key_map=remap)
        if selected:
            self.table.select_book(selected)
            self.show_book(selected)
        elif self._selected_book:
            self.more_btn.configure(state="normal")
        updated = int((payload or {}).get("updated") or 0)
        total = int((payload or {}).get("total") or 0)
        publisher = (payload or {}).get("publisher") or "this publisher"
        if payload and payload.get("cancelled"):
            summary = f"Stopped. Filled {updated} of {total} book(s) from {publisher}."
        elif updated:
            summary = f"Filled missing details for {updated} of {total} book(s) from {publisher}. New fields are highlighted in green."
        else:
            summary = (
                f"No new fields were added for {publisher}. "
                "If a publisher book page was found, its link is in the book details."
            )
        self._set_status(summary)
        self._complete_lookup_popup(summary)
        if selected:
            self._show_found_fields_popup(selected)

    def copy_description(self) -> None:
        text = self._description_text.strip()
        if not text:
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()
        self._set_status("Description copied.")
        self._show_copied_toast()

    def _show_copied_toast(self) -> None:
        if self._copy_toast_after:
            try:
                self.after_cancel(self._copy_toast_after)
            except tk.TclError:
                pass
            self._copy_toast_after = None
        if self._copy_toast is not None:
            try:
                self._copy_toast.destroy()
            except tk.TclError:
                pass
            self._copy_toast = None
        toast = tk.Toplevel(self)
        toast.overrideredirect(True)
        toast.attributes("-topmost", True)
        frame = tk.Frame(toast, bg=NAVY, padx=12, pady=6)
        frame.pack()
        tk.Label(frame, text="Copied", bg=NAVY, fg=WHITE, font=("Segoe UI", 10, "bold")).pack()
        toast.update_idletasks()
        anchor = self.copy_desc_btn
        x = anchor.winfo_rootx() + anchor.winfo_width() - toast.winfo_reqwidth()
        y = anchor.winfo_rooty() + anchor.winfo_height() + 6
        toast.geometry(f"+{max(0, x)}+{max(0, y)}")
        self._copy_toast = toast
        self._copy_toast_after = self.after(1200, self._hide_copied_toast)

    def _hide_copied_toast(self) -> None:
        self._copy_toast_after = None
        if self._copy_toast is not None:
            try:
                self._copy_toast.destroy()
            except tk.TclError:
                pass
            self._copy_toast = None

    def _draw_copy_icon(self, active: bool) -> None:
        canvas = self.copy_desc_btn
        canvas.delete("all")
        color = "#1F3651" if active else "#C5C5C5"
        canvas.create_rectangle(8, 3, 19, 15, outline=color, width=1)
        canvas.create_rectangle(4, 8, 15, 20, outline=color, fill=WHITE, width=1)
        canvas.configure(cursor="hand2" if active else "arrow")

    def _clear_detail_panel(self) -> None:
        for child in self.detail_inner.winfo_children():
            child.destroy()
        self._value_labels = []
        self._detail_row = 0

    def _add_detail_section(self, title: str) -> None:
        tk.Label(
            self.detail_inner,
            text=title,
            bg=WHITE,
            fg=NAVY,
            font=("Segoe UI", 10, "bold"),
            anchor="w",
        ).grid(row=self._detail_row, column=0, columnspan=3, sticky="ew", padx=8, pady=(12, 4))
        self._detail_row += 1

    def _add_detail_row(
        self,
        label: str,
        value: str,
        *,
        is_new: bool = False,
        source: str = "",
        link: str = "",
    ) -> None:
        row = self._detail_row
        tk.Label(
            self.detail_inner,
            text=label,
            bg=WHITE,
            fg="#5A6570",
            font=("Segoe UI", 9),
            anchor="e",
            justify="right",
            wraplength=160,
        ).grid(row=row, column=0, sticky="ne", padx=(8, 0), pady=4)
        empty = not str(value or "").strip()
        if empty:
            display = "—"
            bg, fg = "#EFEBE3", "#A09890"
            font = ("Segoe UI", 10)
        elif is_new:
            display = value
            bg, fg = NEW_BG, NEW_FG
            font = ("Segoe UI", 10)
        else:
            display = value
            bg, fg = "#F4F1EA", "#1B1B1B"
            font = ("Segoe UI", 10)
        if link and not empty:
            fg = "#0B57D0"
            font = ("Segoe UI", 10, "underline")
        val = tk.Label(
            self.detail_inner,
            text=display,
            bg=bg,
            fg=fg,
            font=font,
            anchor="w",
            justify="left",
            padx=10,
            pady=5,
        )
        val.grid(row=row, column=2, sticky="ew", pady=2, padx=(0, 8))
        self._value_labels.append(val)
        if link and not empty:
            val.configure(cursor="hand2")
            val.bind("<Button-1>", lambda _e, target=link: self._open_in_browser(target))
        self._detail_row += 1

    def _set_links(self, items: list[tuple[str, str]]) -> None:
        self._add_detail_section("Book pages")
        if not items:
            self._add_detail_row("Book page", "")
            return
        for label, url in items:
            self._add_detail_row(label, url, link=url)

    def _open_in_browser(self, url: str) -> None:
        if not url:
            return
        data = load_config()
        browser_id = str(data.get("browser") or "chrome")
        label = browser_label(browser_id)
        executable = browser_executable(browser_id, str(data.get("browser_path") or ""))
        try:
            if executable:
                subprocess.Popen([executable, url], close_fds=True)
            else:
                webbrowser.open(url)
                if browser_id != "system":
                    label = "the system browser"
            self._set_status(f"Opened in {label}: {url}")
        except Exception as exc:
            messagebox.showerror("Could not open browser", str(exc))

    def _set_details(self, book: Book, fields: dict[str, str]) -> None:
        new_fields = {part for part in (book.extra.get("new_fields") or "").split(",") if part}
        note = (book.extra.get("lookup_note") or "").strip()
        captured = book.captured_fields()
        shown: set[str] = set()
        self._clear_detail_panel()
        self._add_detail_section("Scan")
        self._add_detail_row("Scanner ID", book.scanner_id or "—")
        workflow = "Final" if book.final else "Approved" if book.approved else "In Excel" if book.excel_passed else "Not approved"
        self._add_detail_row("Workflow", workflow)
        self._add_detail_row("Status", book.scan_status or "—")
        self._add_detail_row("Message", book.scan_message or "—")
        if note:
            tk.Label(
                self.detail_inner,
                text=note,
                bg=WHITE,
                fg=NEW_FG,
                font=("Segoe UI", 9),
                anchor="w",
                justify="left",
                wraplength=420,
            ).grid(row=self._detail_row, column=0, columnspan=3, sticky="ew", padx=8, pady=(4, 6))
            self._detail_row += 1
        self._add_detail_section("Catalog fields")

        def add_field(key: str, label: str, value: str) -> None:
            is_new = key in new_fields or book.is_external_source(_source_field_key(key))
            link = value if key in {"cover_image_url", "back_image_url"} and str(value or "").startswith("http") else ""
            self._add_detail_row(label, value, is_new=is_new, link=link)

        columns = self.excel_columns or []
        if columns:
            for col in columns:
                field = col.get("field") or ""
                header = str(col.get("header") or "")
                if field == "description_he":
                    shown.add(field)
                    continue
                if field == "scanner_id":
                    shown.add(field)
                    continue
                value = fields.get(field, "") if field else ""
                if not value and field:
                    value = captured.get(field, "")
                if field == "price_ils":
                    value = format_price(value)
                if field == "language":
                    from field_map import isolate_language

                    value = isolate_language(value)
                add_field(field or header, header, value)
                if field:
                    shown.add(field)
        else:
            add_field("title", "Title (Hebrew)", fields.get("title_he", ""))
            add_field("publisher", "Publisher", fields.get("publisher", ""))
            add_field("isbn", "ISBN", fields.get("isbn", ""))
            add_field("year", "Copyright year", fields.get("year", ""))
            add_field("pages", "Number of pages", fields.get("pages", ""))
            add_field("price_ils", "Israeli price (Shekel)", format_price(fields.get("price_ils", "")))

        extra_rows: list[tuple[str, str, str]] = []
        for col in self.excel_all_columns:
            field = col.get("field") or ""
            if col.get("colored") or not field or field in shown:
                continue
            value = fields.get(field) or captured.get(field) or ""
            if field == "language":
                from field_map import isolate_language

                value = isolate_language(value)
            if value:
                extra_rows.append((field, str(col["header"]), value))
                shown.add(field)
        for name, value in captured.items():
            if name in shown or not value:
                continue
            if name == "language":
                from field_map import isolate_language

                value = isolate_language(value)
                if not value:
                    continue
            header = "Danacode (short)" if name == "cat_number" else name
            extra_rows.append((name, header, value))
            shown.add(name)
        short = book.danacode_short()
        if short and "cat_number" not in shown:
            extra_rows.append(("cat_number", "Danacode (short)", short))
            shown.add("cat_number")
        if extra_rows:
            self._add_detail_section("Other catalog fields")
            for key, header, value in extra_rows:
                add_field(key, header, value)

        leftover = book.unmatched_page_fields()
        if leftover and not book.publisher_found_fields():
            self._add_detail_section("Also on the book page")
            for label, value in leftover[:20]:
                self._add_detail_row(label, value)

    def _set_description(self, text: str) -> None:
        self._description_text = text or ""
        self._draw_copy_icon(bool(self._description_text.strip()))
        self.desc_view.set_text(self._description_text)

    def _on_filter_change(self, _event=None) -> None:
        keys = {key for key, var in self._filter_vars.items() if var.get()}
        self.table.set_filters(keys)
        if not keys:
            self._set_status("Showing all books.")
            return
        labels = [label for key, label in ROW_STATUSES if key in keys]
        self._set_status("Showing: " + ", ".join(labels).lower() + ".")

    def clear_books_with_keep(self) -> None:
        if self._busy:
            return
        if self._guard_locked("clear books from this list"):
            return
        if not self.books:
            messagebox.showinfo("Clear books", "This list is already empty.")
            return
        win = tk.Toplevel(self)
        win.title("Clear books")
        win.configure(bg=BG)
        win.transient(self)
        win.grab_set()
        win.resizable(False, False)
        body = ttk.Frame(win, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(
            body,
            text="Choose the row statuses to keep. Books that do not match any of these will be removed from this list (not from Excel).",
            wraplength=420,
        ).pack(anchor="w")
        keep_vars: dict[str, tk.BooleanVar] = {}
        box = ttk.Frame(body)
        box.pack(anchor="w", pady=(10, 8))
        defaults = {"checked", "approved", "final"}
        for key, label in ROW_STATUSES:
            var = tk.BooleanVar(value=key in defaults)
            keep_vars[key] = var
            ttk.Checkbutton(box, text=f"Keep {label.lower()}", variable=var).pack(anchor="w", pady=2)

        def cancel() -> None:
            win.grab_release()
            win.destroy()

        def confirm() -> None:
            keep = {key for key, var in keep_vars.items() if var.get()}
            remaining = [
                book
                for book in self.books
                if keep and any(self.table.book_has_status(book, key) for key in keep)
            ]
            removed = len(self.books) - len(remaining)
            if removed <= 0:
                messagebox.showinfo("Clear books", "Nothing to remove with those keep choices.")
                cancel()
                return
            if not keep:
                extra = f"No statuses are marked to keep, so all {len(self.books)} book(s) will be removed."
            else:
                labels = [label for key, label in ROW_STATUSES if key in keep]
                extra = f"Keep {', '.join(labels).lower()}. Remove {removed} book(s)."
            if not messagebox.askyesno("Clear books", extra + "\n\nContinue?"):
                return
            selected = self._selected_book
            self.books = remaining
            self._prepare_books(self.books)
            self.table.set_books(self.books, keep_checks=True)
            if selected not in self.books:
                self._selected_book = None
            self._persist_working()
            self._refresh_list_status()
            self._set_status(f"Removed {removed} book(s). {len(self.books)} remaining.")
            cancel()

        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(8, 0))
        ttk.Button(btns, text="Cancel", command=cancel).pack(side="right")
        ttk.Button(btns, text="Clear others", command=confirm).pack(side="right", padx=(0, 8))
        win.protocol("WM_DELETE_WINDOW", cancel)
        win.lift()
        win.focus_force()

    def _prepare_books(self, books: list[Book]) -> None:
        for book in books:
            if book.title:
                book.refresh_scan_status()
            elif not book.scan_status:
                book.scan_status = "failed"
                if not book.scan_message:
                    book.scan_message = "Scan did not complete."
        attach_books(books)

    def _update_workflow_buttons(self, book: Book | None) -> None:
        if book is None or self._busy or self._list_locked:
            self.approve_btn.configure(state="disabled")
            self.final_btn.configure(state="disabled")
            return
        can_approve = bool(book) and (not book.final) and (not book.excel_passed) and book.workflow_label() == "Approve"
        can_final = bool(book) and (not book.final) and book.excel_passed
        self.approve_btn.configure(state="normal" if can_approve else "disabled")
        self.final_btn.configure(state="normal" if can_final else "disabled")

    def handle_book_action(self, book: Book) -> None:
        if self._guard_locked("change books on this list"):
            return
        label = book.workflow_label()
        if label == "Approve":
            self._approve_books([book])
        elif label == "Mark final":
            self._mark_books_final([book])

    def approve_selected(self) -> None:
        if self._guard_locked("approve books"):
            return
        if self._selected_book:
            self._approve_books([self._selected_book])

    def approve_checked(self) -> None:
        if self._guard_locked("approve books"):
            return
        selected = self.table.selected_books()
        if not selected:
            messagebox.showinfo("Nothing selected", "Check one or more books in the list first.")
            return
        self._approve_books(selected)

    def mark_selected_final(self) -> None:
        if self._guard_locked("mark books final"):
            return
        if self._selected_book:
            self._mark_books_final([self._selected_book])

    def mark_selected_final_checked(self) -> None:
        if self._guard_locked("mark books final"):
            return
        selected = self.table.selected_books()
        if not selected:
            messagebox.showinfo("Nothing selected", "Check one or more approved books first.")
            return
        self._mark_books_final(selected)

    def _approve_books(self, books: list[Book]) -> None:
        to_write: list[Book] = []
        skipped_final = 0
        skipped_failed = 0
        already = 0
        for book in books:
            attach_book(book)
            if book.final:
                skipped_final += 1
                continue
            if book.scan_status == "failed" and not (book.title or "").strip():
                skipped_failed += 1
                continue
            if book.excel_passed:
                book.approved = True
                persist_book_state(book)
                already += 1
            else:
                to_write.append(book)
        written = 0
        skipped_excel = 0
        saved = None
        note = ""
        if to_write:
            written, skipped_excel, saved, note = self._pass_books_to_excel(to_write)
        for book in books:
            self.table.refresh_book(book)
        if self._selected_book:
            self.show_book(self._selected_book)
        self._persist_working()
        parts = []
        if written:
            parts.append(f"Approved and passed {written} book(s) to Excel")
        if already:
            parts.append(f"{already} already in Excel")
        if skipped_excel:
            parts.append(f"{skipped_excel} already in Excel (duplicate)")
        if skipped_failed:
            parts.append(f"{skipped_failed} failed scan(s) need a title first")
        if skipped_final:
            parts.append(f"{skipped_final} already final")
        summary = "; ".join(parts) or "Nothing to approve."
        if saved and note:
            summary += note
        self._set_status(summary)
        if written or already or skipped_excel:
            messagebox.showinfo("Approved", summary)

    def _mark_books_final(self, books: list[Book]) -> None:
        marked = 0
        skipped = 0
        for book in books:
            if book.final:
                continue
            if not book.excel_passed:
                skipped += 1
                continue
            book.final = True
            book.approved = True
            persist_book_state(book)
            self.table.refresh_book(book)
            marked += 1
        if self._selected_book:
            self.show_book(self._selected_book)
        self._persist_working()
        if not marked:
            messagebox.showinfo(
                "Mark final",
                "Pass the book to Excel first by clicking Approve. After you process the spreadsheet, mark it final so it will not be written again.",
            )
            return
        extra = f" {skipped} book(s) are not in Excel yet." if skipped else ""
        self._set_status(f"Marked {marked} book(s) final.{extra}")
        self._persist_working()

    def _pass_books_to_excel(self, books: list[Book]) -> tuple[int, int, Path | None, str]:
        path = Path(self.excel_path.get().strip())
        if not path.exists():
            messagebox.showerror("Excel", "The Excel file was not found.")
            return 0, 0, None, ""
        try:
            catalog = CatalogWorkbook(path)
            mark_excel_ids(catalog.existing_scanner_ids())
            payload = [book.to_excel_fields() for book in books]
            written, skipped, written_ids, skipped_ids = catalog.append_books(payload)
            saved = catalog.save()
        except Exception as exc:
            messagebox.showerror("Could not write Excel", str(exc))
            return 0, 0, None, ""
        done = set(written_ids) | set(skipped_ids)
        for book in books:
            if book.scanner_id in done:
                book.approved = True
                book.excel_passed = True
                persist_book_state(book)
        note = ""
        if saved != path:
            note = f"\n\nThe original file is open or locked, so results were saved as:\n{saved}"
        return written, skipped, saved, note

    def write_excel(self) -> None:
        self.approve_checked()

    def _set_status(self, text: str) -> None:
        self.status.set(text)

    def check_for_updates(self, silent: bool = False) -> None:
        if self._updating:
            return
        if self._busy:
            if not silent:
                messagebox.showinfo(
                    "Update",
                    "Wait until Search or More finishes, then check for updates again.",
                )
            else:
                self.after(8000, lambda: self.check_for_updates(silent=True))
            return
        thread = threading.Thread(target=self._run_update_check, args=(silent,), daemon=True)
        thread.start()

    def _run_update_check(self, silent: bool) -> None:
        from app_update import UpdateError, check_for_update

        try:
            info = check_for_update()
        except UpdateError as exc:
            self._ui_queue.put(("update_error", (silent, str(exc))))
            return
        except Exception as exc:
            self._ui_queue.put(("update_error", (silent, str(exc))))
            return
        self._ui_queue.put(("update_result", (silent, info)))

    def _on_update_result(self, silent: bool, info: object) -> None:
        if info is None:
            if silent:
                return
            messagebox.showinfo("Update", "SISU is already up to date.")
            self._set_status("SISU is already up to date.")
            return
        if silent and self._update_declined:
            self._set_status("A newer SISU version is available. Click Check for updates when you are ready.")
            return
        from app_update import UpdateInfo

        if not isinstance(info, UpdateInfo):
            return
        if info.dirty:
            messagebox.showwarning(
                "Cannot update automatically",
                "A newer SISU version is available, but this copy has local file changes "
                "so it cannot update itself.\n\nUpdate this folder by hand, or wait until local changes are cleared.",
            )
            return
        if not messagebox.askokcancel(
            "Update SISU",
            "A newer version of SISU is available.\n\n"
            "If you continue, SISU will:\n"
            "• download the update from GitHub\n"
            "• install any new libraries\n"
            "• close this window and start the new version\n\n"
            "Finish any work you want to keep first.\n\n"
            "Update and restart now?",
        ):
            self._update_declined = True
            self._set_status("Update postponed. SISU will keep this version until you choose Check for updates.")
            return
        self._apply_self_update()

    def _on_update_error(self, silent: bool, text: str) -> None:
        self._updating = False
        if self._busy:
            return
        self.search_btn.configure(state="normal")
        if silent:
            self._set_status("Could not check for SISU updates right now.")
            return
        messagebox.showerror("Update failed", text)

    def _apply_self_update(self) -> None:
        self._updating = True
        self.search_btn.configure(state="disabled")
        self.more_btn.configure(state="disabled")
        self.approve_btn.configure(state="disabled")
        self.final_btn.configure(state="disabled")
        self._set_status("Updating SISU from GitHub. The window will restart when it is done.")
        thread = threading.Thread(target=self._run_self_update, daemon=True)
        thread.start()

    def _run_self_update(self) -> None:
        from app_update import UpdateError, apply_update

        try:
            apply_update()
        except UpdateError as exc:
            self._ui_queue.put(("update_error", (False, str(exc))))
            return
        except Exception as exc:
            self._ui_queue.put(("update_error", (False, str(exc))))
            return
        self._ui_queue.put(("update_applied", None))

    def _finish_self_update(self) -> None:
        messagebox.showinfo(
            "Update complete",
            "SISU has been updated. This window will close and the new version will open.",
        )
        self._restart_app("Restarting the updated SISU…")

    def _watch_for_reload(self) -> None:
        if self._busy or self._updating:
            self.after(1000, self._watch_for_reload)
            return
        current = _source_mtimes()
        sentinel = RELOAD_SENTINEL.exists()
        if sentinel:
            try:
                RELOAD_SENTINEL.unlink()
            except OSError:
                pass
        if sentinel or current != self._mtimes:
            self._hot_replace()
            return
        self.after(800, self._watch_for_reload)

    def _hot_replace(self) -> None:
        self._restart_app("Code changed — restarting window, cache kept…")

    def _restart_app(self, reason: str = "") -> None:
        if reason:
            self._set_status(reason)
        self.update_idletasks()
        from app_update import restart_process

        restart_process()
        self.after(150, self.destroy)


def _book_urls(book: Book) -> list[str]:
    urls: list[str] = []
    for value in [book.url, *(book.extra.get("sources") or "").split("|")]:
        url = (value or "").strip()
        if url and url not in urls:
            urls.append(url)
    return urls


def _source_field_key(key: str) -> str:
    mapping = {
        "title_he": "title",
        "author_he": "author",
        "description_he": "description",
    }
    return mapping.get(key, key)


def _field_source_label(book: Book, key: str) -> str:
    if key == "size":
        names: list[str] = []
        for name in ("height_cm", "width_cm", "thickness_cm"):
            label = book.source_display(name)
            if label and label not in names:
                names.append(label)
        return ", ".join(names)
    return book.source_display(_source_field_key(key))


def _book_link_items(book: Book) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(label: str, url: str) -> None:
        value = (url or "").strip()
        if not value or value in seen:
            return
        seen.add(value)
        items.append((label, value))

    add("Publisher site", book.extra.get("publisher_site") or resolve_publisher_site(book.publisher) or "")
    add("Publisher book page", book.extra.get("publisher_page") or "")
    if book.url:
        add(f"{site_display_name(book.url)} book page", book.url)
    primary = site_host(book.url)
    for host, url in sorted(book.site_pages().items(), key=lambda item: item[0]):
        if host == primary:
            continue
        add(f"{site_display_name(url)} book page", url)
    return items


def _source_mtimes() -> dict[str, float]:
    stamps: dict[str, float] = {}
    for name in WATCHED_FILES:
        path = APP_DIR / name
        if path.exists():
            stamps[name] = path.stat().st_mtime
    return stamps


def _enable_windows_dpi() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


if __name__ == "__main__":
    _enable_windows_dpi()
    app = BookCatalogApp()
    app.mainloop()
