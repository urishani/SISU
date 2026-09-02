"""SISU book catalog filler — crawl a bookstore and write colored Excel columns."""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, simpledialog, ttk

from urllib.parse import quote, urlparse

from dataclasses import asdict

from activity_log import ActivityLog
from app_config import (
    BROWSERS,
    LLM_DEFAULT_BASE_URLS,
    LLM_DEFAULT_MODELS,
    LLM_SERVICES,
    browser_executable,
    browser_label,
    load_config,
    merged_publisher_rows,
    normalize_site_url,
    save_config,
)
from book_crawler import (
    Book,
    BookCrawler,
    CrawlCancelled,
    CrawlReport,
    books_match,
    catalog_listing_url,
    dedupe_book_list,
    entry_now,
    fill_missing_entry_dates,
    fill_missing_phonetics,
    format_entry_stamp,
    format_person_name,
    format_price,
    listing_url_key,
    merge_later_into,
    parse_site_urls,
    site_display_name,
    site_host,
)
from book_table import ROW_STATUSES, BookTable
from catalog_excel import CatalogWorkbook, ensure_list_workbook, list_excel_filename
from field_map import ALIASES_PATH, EXCEL_TARGETS, reload_aliases, write_field_report
from hebrew_view import HebrewDescription
from publisher_sites import publishers_match, resolve_publisher_site
from scanner_registry import attach_book, attach_books, persist_book_state
from app_update import read_app_version
from scan_lists import (
    books_from_payload,
    build_payload,
    default_scan_title,
    DEFAULT_SEARCH_URLS,
    delete_named,
    empty_payload,
    list_summaries,
    load_named,
    load_stash,
    load_working,
    merge_search_urls,
    rename_named,
    save_named,
    save_stash,
    save_working,
    set_named_flags,
    stash_exists,
    stash_has_data,
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
APP_NAME = "SISU Book Catalog Filler"


def app_title() -> str:
    extra = read_app_version().label()
    if extra:
        return f"{APP_NAME} ({extra})"
    return APP_NAME


class HoverTip:
    """Callout that names a button or card when the pointer rests on it."""

    def __init__(self, widget: tk.Misc, text: str) -> None:
        self.widget = widget
        self.text = (text or "").strip()
        self._after: str | None = None
        self._tip: tk.Toplevel | None = None
        if not self.text:
            return
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._cancel()
        try:
            self._after = self.widget.after(450, self._show)
        except tk.TclError:
            self._after = None

    def _cancel(self) -> None:
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None

    def _show(self) -> None:
        self._after = None
        if self._tip is not None or not self.widget.winfo_ismapped():
            return
        try:
            x = self.widget.winfo_rootx() + 8
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        except tk.TclError:
            return
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{x}+{y}")
        try:
            tip.wm_attributes("-topmost", True)
        except tk.TclError:
            pass
        tk.Label(
            tip,
            text=self.text,
            justify="left",
            wraplength=320,
            background="#FFF8E8",
            foreground=NAVY,
            relief="solid",
            borderwidth=1,
            font=("Segoe UI", 9),
            padx=8,
            pady=6,
        ).pack()
        self._tip = tip


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
ERROR_FG = "#B42318"
ERROR_BG = "#FDECEC"
FINAL_FG = "#0B57D0"
FINAL_BG = "#E8F0FE"
DEFAULT_URLS = "\n".join(DEFAULT_SEARCH_URLS)
UPDATE_CHECK_FIRST_MS = 2_000
UPDATE_CHECK_EVERY_MS = 15 * 60 * 1000


class BookCatalogApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(app_title())
        self.geometry("1280x820")
        self.minsize(1100, 720)
        self.configure(bg=BG)

        self.excel_path = tk.StringVar(value="")
        self._excel_dir = Path(str(load_config().get("excel_dir") or "").strip() or APP_DIR)
        self.year = tk.StringVar(value="2026")
        self.max_pages = tk.IntVar(value=40)
        self.no_page_limit = tk.BooleanVar(value=False)
        self._pages_total = 0
        self.include_unknown = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="Start a new list, or open a saved scan list.")
        self.work_hint = tk.StringVar(value="")
        self.scan_live = tk.StringVar(value="")
        self.summary = tk.StringVar(value="")
        self.table_counts = tk.StringVar(value="0 selected · 0 shown · 0 total")
        self._progress_determinate = True
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
        self._skip_cache_restore = False
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
        self._report_popup: tk.Toplevel | None = None
        self._report_view: tk.Text | None = None
        self._updating = False
        self._update_check_running = False
        self._update_declined_remote = ""
        self._update_check_after: str | int | None = None
        self._cancel = threading.Event()
        self._busy = False
        self._follow_search = True
        self._programmatic_select = False
        self._detail_job: str | None = None
        self._pending_detail: Book | None = None
        self._scan_site = ""
        self._scan_found = 0
        self._scan_checking = 0
        self._scan_total = 0
        self._scan_phase = ""
        self._scan_book = ""
        self._activity = ActivityLog()
        self._log_popup: tk.Toplevel | None = None
        self._log_list: tk.Listbox | None = None
        self._log_view: tk.Text | None = None
        self._log_runs: list[dict[str, str]] = []
        self._log_selected_id = ""
        self._log_refresh_job: str | None = None
        self._live_save_job: str | None = None
        self._ui_queue: queue.Queue = queue.Queue()
        self._clean_fingerprint = ""
        self._list_actions_ready = False

        self._setup_style()
        self._build()
        self.refresh_excel_info()
        self._restore_working_list()
        self._list_actions_ready = True
        self._refresh_list_status()
        self._refresh_selection_label()
        self._mtimes = _source_mtimes()
        self.after(120, self._drain_queue)
        self.after(900, self._watch_for_reload)
        self.after(UPDATE_CHECK_FIRST_MS, self._periodic_update_check)

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
        try:
            style.map(
                "Accent.TButton",
                foreground=[("disabled", "#8A8A8A"), ("!disabled", NAVY)],
            )
        except tk.TclError:
            pass

    def _build(self) -> None:
        header = tk.Frame(self, bg=NAVY)
        header.pack(fill="x")
        ttk.Label(header, text=APP_NAME, style="Header.TLabel").pack(
            side="left", padx=(18, 0), pady=8
        )
        version_text = read_app_version().label()
        if version_text:
            ttk.Label(header, text=f"({version_text})", style="Sub.TLabel").pack(
                side="left", padx=(10, 12), pady=10
            )
        ttk.Label(
            header,
            text="Crawl sites, then fill orange Excel columns",
            style="Sub.TLabel",
        ).pack(side="left", padx=(0, 12), pady=10)
        header_btns = tk.Frame(header, bg=NAVY)
        header_btns.pack(side="right", padx=12, pady=6)

        def header_button(text: str, command, tip: str) -> tk.Button:
            btn = tk.Button(
                header_btns,
                text=text,
                command=command,
                bg="#E8D5B5",
                fg=NAVY,
                activebackground=WHITE,
                activeforeground=NAVY,
                relief="flat",
                bd=0,
                font=("Segoe UI", 9, "bold"),
                padx=10,
                pady=4,
                cursor="hand2",
            )
            btn.pack(side="left", padx=4)
            self._callout(btn, tip)
            return btn

        header_button(
            "Check for updates",
            lambda: self.check_for_updates(silent=False),
            "Look on GitHub for a newer SISU and offer to install it.",
        )
        header_button(
            "Field report",
            self.open_field_report,
            "Show which page labels matched the Excel columns, and which still need aliases.",
        )
        header_button(
            "Lists",
            self.open_lists_manager,
            "Open, rename, or delete saved scan lists.",
        )
        header_button("Settings", self.open_settings, "Publisher websites, field aliases, the browser to open, and LLM.")
        self.after_idle(self._fit_search_fields)

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=12, pady=8)
        self._layout_body = body
        body.columnconfigure(0, weight=1)
        body.rowconfigure(3, weight=1)

        lists = ttk.LabelFrame(body, text="Scan list", padding=(8, 4, 8, 6))
        lists.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self._callout(
            lists,
            "This card is the scan list you are working on: its title, save/open actions, and whether it is locked.",
        )
        lists.columnconfigure(1, weight=2)
        lists.columnconfigure(3, weight=1)
        ttk.Label(lists, text="Title").grid(row=0, column=0, sticky="e", padx=(0, 8))
        self.list_title_entry = ttk.Entry(lists, textvariable=self.list_title)
        self.list_title_entry.grid(row=0, column=1, sticky="ew")
        self.list_title_entry.bind("<FocusOut>", lambda _e: self._on_list_title_change())
        self._callout(self.list_title_entry, "Name of this scan list. It is also used in the Excel file name.")
        list_btns = ttk.Frame(lists)
        list_btns.grid(row=0, column=2, sticky="w", padx=(8, 8))
        new_btn = ttk.Button(list_btns, text="New", command=self.new_working_list)
        new_btn.pack(side="left", padx=(0, 4))
        self._callout(new_btn, "Start a fresh working list. The current unsaved list can be stashed first if you need it.")
        self.stash_btn = ttk.Button(list_btns, text="Stash", command=self.stash_working_list)
        self.stash_btn.pack(side="left", padx=(0, 4))
        self._callout(
            self.stash_btn,
            "Put a nameless working list aside. Disabled when this list already has a name — save it instead.",
        )
        self.restore_btn = ttk.Button(list_btns, text="Restore stash", command=self.restore_stash)
        self.restore_btn.pack(side="left", padx=(0, 4))
        self._callout(
            self.restore_btn,
            "Bring back the stashed list. Disabled when the stash is empty.",
        )
        self.save_btn = ttk.Button(list_btns, text="Save list", command=self.save_current_list)
        self.save_btn.pack(side="left", padx=(0, 4))
        self._callout(
            self.save_btn,
            "Highlighted when this list has unsaved changes. Disabled when there is nothing new to save.",
        )
        open_lists_btn = ttk.Button(list_btns, text="Open lists…", command=self.open_lists_manager)
        open_lists_btn.pack(side="left")
        self._callout(open_lists_btn, "Browse saved scan lists.")
        self.list_status_label = ttk.Label(lists, textvariable=self.list_status, wraplength=1, justify="left")
        self.list_status_label.grid(row=0, column=3, sticky="ew")
        self._callout(self.list_status_label, "Whether this list is new, saved, locked, or archived.")

        form = ttk.LabelFrame(body, text="Search", padding=(8, 4, 8, 6))
        form.grid(row=1, column=0, sticky="ew")
        self._callout(
            form,
            "Search every bookstore and catalog URL for this publication year, list all titles, then fill book and publisher pages.",
        )
        form.columnconfigure(1, weight=0)
        form.columnconfigure(3, weight=1)

        ttk.Label(form, text="List Excel").grid(row=0, column=0, sticky="e", padx=(0, 8), pady=(0, 4))
        self.excel_entry = ttk.Entry(form, textvariable=self.excel_path, state="readonly", width=52)
        self.excel_entry.grid(row=0, column=1, sticky="w", pady=(0, 4))
        self._callout(self.excel_entry, "Excel file for this scan list. Final books are written here.")
        excel_btns = ttk.Frame(form)
        excel_btns.grid(row=0, column=2, padx=(8, 0), pady=(0, 4), sticky="w")
        self.excel_folder_btn = ttk.Button(excel_btns, text="Folder…", command=self.browse_excel_folder)
        self.excel_folder_btn.pack(side="left")
        self._callout(self.excel_folder_btn, "Choose the folder where this list’s Excel file is kept.")
        self.excel_open_btn = ttk.Button(excel_btns, text="Open", command=self.open_excel)
        self.excel_open_btn.pack(side="left", padx=(6, 0))
        self._callout(self.excel_open_btn, "Open this list’s Excel file.")
        self.excel_share_btn = ttk.Button(excel_btns, text="Share", command=self.share_excel)
        self.excel_share_btn.pack(side="left", padx=(6, 0))
        self._callout(self.excel_share_btn, "Start an email with this list’s Excel file attached.")

        ttk.Label(form, text="Site URLs").grid(row=1, column=0, sticky="ne", padx=(0, 8), pady=(2, 0))
        url_wrap = ttk.Frame(form)
        url_wrap.grid(row=1, column=1, sticky="nw", pady=(2, 0))
        url_wrap.columnconfigure(0, weight=0)
        url_wrap.rowconfigure(0, weight=1)
        self.url_text = tk.Text(url_wrap, height=3, width=52, wrap="none", font=("Segoe UI", 10), undo=True, padx=6, pady=2)
        url_scroll_y = ttk.Scrollbar(url_wrap, orient="vertical", command=self.url_text.yview)
        self.url_text.configure(yscrollcommand=url_scroll_y.set)
        self.url_text.grid(row=0, column=0, sticky="nw")
        url_scroll_y.grid(row=0, column=1, sticky="ns")
        self.url_text.insert("1.0", DEFAULT_URLS)
        self.url_text.tag_configure("searching", background="#FDE6C4", foreground="#9A3412")
        self.url_text.bind("<<Modified>>", self._on_url_text_modified)
        self._callout(
            self.url_text,
            "One bookstore or catalog URL per line. Search reads all of them, merges unique titles, and does not stop at the first list.",
        )
        self.excel_path.trace_add("write", lambda *_args: self._fit_search_fields())

        search_side = ttk.Frame(form)
        search_side.grid(row=1, column=2, sticky="nw", padx=(10, 0), pady=(2, 0))
        year_row = ttk.Frame(search_side)
        year_row.pack(anchor="w")
        ttk.Label(year_row, text="Year").pack(side="left")
        self.year_entry = ttk.Entry(year_row, textvariable=self.year, width=8)
        self.year_entry.pack(side="left", padx=(6, 10))
        self._callout(self.year_entry, "Keep books with this publication year, for example 2026.")
        self.pages_label = ttk.Label(year_row, text="Max pages")
        self.pages_label.pack(side="left")
        self.pages_spin = ttk.Spinbox(year_row, from_=1, to=10000, textvariable=self.max_pages, width=5)
        self.pages_spin.pack(side="left", padx=(6, 6))
        self._callout(
            self.pages_spin,
            "How many catalog listing pages to read on each site. The number in parentheses is the site’s total pages once Search finds it.",
        )
        self.no_limit_check = ttk.Checkbutton(
            year_row, text="No limit", variable=self.no_page_limit, command=self._on_page_limit_toggle
        )
        self.no_limit_check.pack(side="left")
        self._callout(
            self.no_limit_check,
            "Read every catalog page on each site. Turn this off to cap Search at the Max pages number.",
        )
        self.unknown_check = ttk.Checkbutton(
            search_side, text="Also keep books with no year listed", variable=self.include_unknown
        )
        self.unknown_check.pack(anchor="w", pady=(6, 6))
        self._callout(self.unknown_check, "If a book page has no year, still keep it in the list.")
        buttons = ttk.Frame(search_side)
        buttons.pack(anchor="w")
        self.search_btn = ttk.Button(buttons, text="Search", command=self.start_search, style="Accent.TButton")
        self.search_btn.pack(side="left", padx=(0, 4))
        self._callout(
            self.search_btn,
            "Search every bookstore URL, merge unique titles into one list, then fill catalog and publisher pages.",
        )
        self.log_btn = ttk.Button(buttons, text="Show log", command=self.show_activity_log)
        self.log_btn.pack(side="left", padx=(0, 4))
        self._callout(
            self.log_btn,
            "Open the log of this search and earlier searches. Progress lines such as checking 48 of 1,728 are updated in place.",
        )
        self.stop_btn = ttk.Button(buttons, text="Stop", command=self.stop_search, state="disabled")
        self.stop_btn.pack(side="left")
        self._callout(self.stop_btn, "Stop the current search. Books found so far are kept.")

        self.colored_info_label = ttk.Label(form, textvariable=self.colored_info, wraplength=1, justify="left")
        self.colored_info_label.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(6, 0))

        self.summary_label = ttk.Label(body, textvariable=self.summary, wraplength=1, justify="left")
        self.summary_label.grid(row=2, column=0, sticky="ew", pady=(4, 4))

        split = ttk.Panedwindow(body, orient="horizontal")
        list_frame = ttk.Frame(split)
        detail_frame = ttk.Frame(split, style="Card.TFrame")
        split.add(list_frame, weight=3)
        split.add(detail_frame, weight=2)
        self._callout(
            list_frame,
            "Books found for this year. Click a row for details. Click the ☑ column to select books.",
        )
        self._callout(
            detail_frame,
            "Details for the book you clicked: catalog fields, publisher page, approve, and mark final.",
        )

        self.table = BookTable(
            list_frame,
            on_select=self.show_book,
            on_check=self._on_check_change,
            on_publisher=self.ask_publisher_lookup,
        )
        filter_bar = ttk.Frame(list_frame)
        filter_bar.pack(fill="x", pady=(0, 4))
        ttk.Label(filter_bar, text="Filter").pack(side="left")
        self._filter_vars: dict[str, tk.BooleanVar] = {}
        filter_tips = {
            "important": "Show books that still need attention: created and not passed to the database, updated after they were created, or updated after they were passed.",
            "checked": "Show only books you ticked in the ☑ column.",
            "approved": "Show only books you approved.",
            "failed": "Show only books that had an error.",
            "successful": "Show only books that were read successfully.",
            "fully scanned": "Show only books with every fillable field set.",
            "final": "Show only books marked final (written to this list’s Excel).",
            "excel": "Show only books that are already in this list’s Excel.",
        }
        for key, label in ROW_STATUSES:
            var = tk.BooleanVar(value=False)
            self._filter_vars[key] = var
            box = ttk.Checkbutton(filter_bar, text=label, variable=var, command=self._on_filter_change)
            box.pack(side="left", padx=(8, 0))
            self._callout(box, filter_tips.get(key, f"Show only {label.lower()} books."))
        clear_btn = ttk.Button(filter_bar, text="Clear books…", command=self.clear_books_with_keep)
        clear_btn.pack(side="right")
        self._callout(clear_btn, "Remove books from this list, while keeping chosen statuses.")
        counts_label = ttk.Label(filter_bar, textvariable=self.table_counts)
        counts_label.pack(side="right", padx=(10, 8))
        self._callout(
            counts_label,
            "How many books are ticked, how many are visible with the current filters, and how many are on the whole list.",
        )
        clear_sel_btn = ttk.Button(filter_bar, text="Clear selection", command=self.table.clear_selection)
        clear_sel_btn.pack(side="right", padx=(6, 0))
        self._callout(clear_sel_btn, "Clear the ☑ ticks. This does not delete books from the list.")
        select_all_btn = ttk.Button(filter_bar, text="Select all", command=self.table.select_all)
        select_all_btn.pack(side="right", padx=(8, 0))
        self._callout(select_all_btn, "Tick every book currently shown in the table (after filters).")
        self.table.pack(fill="both", expand=True)
        self._callout(self.table, "The book table. Click a title to open it on the right.")
        self._callout(self.table.tree, "Click a book row to see its fields. Click ☑ to select it. Click the header ☑ to select all or clear. Click the publisher to look it up.")
        self._callout(self.table.top_btn, "Jump to the first book in the list. Ctrl+Home does the same.")
        self._callout(self.table.bottom_btn, "Jump to the last book in the list. Ctrl+End does the same.")
        self._callout(
            self.table.window_caption,
            "Which rows are in view: first row, last row, and how many rows fit on screen.",
        )
        self.bind_all("<Control-Home>", self._on_list_ctrl_home, add="+")
        self.bind_all("<Control-End>", self._on_list_ctrl_end, add="+")

        detail_header = ttk.Frame(detail_frame, style="Card.TFrame")
        detail_header.pack(fill="x", padx=12, pady=(10, 6))
        ttk.Label(detail_header, text="Selected book", style="Card.TLabel", font=("Segoe UI", 11, "bold")).pack(
            side="left"
        )
        self.unfinal_btn = ttk.Button(
            detail_header, text="Remove final", command=self.remove_selected_final, state="disabled"
        )
        self.unfinal_btn.pack(side="right")
        self._callout(self.unfinal_btn, "Take this book off this list’s Excel and return it to Approved.")
        self.final_btn = ttk.Button(detail_header, text="Mark final", command=self.mark_selected_final, state="disabled")
        self.final_btn.pack(side="right", padx=(0, 6))
        self._callout(self.final_btn, "Write this book to this list’s Excel and mark it Final.")
        self.approve_btn = ttk.Button(detail_header, text="Approve", command=self.approve_selected, state="disabled")
        self.approve_btn.pack(side="right", padx=(0, 6))
        self._callout(self.approve_btn, "Mark this book Approved. It is not written to Excel until you mark it Final.")
        self.more_btn = ttk.Button(detail_header, text="More", command=self.lookup_more, state="disabled")
        self.more_btn.pack(side="right", padx=(0, 6))
        self._callout(self.more_btn, "Search the publisher website again for missing fields on this book.")

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
        self.detail_inner.columnconfigure(1, minsize=50)
        self.detail_inner.columnconfigure(2, weight=1)
        self._value_labels: list[tk.Label] = []
        self._expanded_detail_keys: set[str] = set()

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
        self._callout(self.copy_desc_btn, "Copy the description to the clipboard.")
        self._callout(desc_pane, "The book description from the catalog or publisher page.")
        self._draw_copy_icon(False)
        desc_wrap = tk.Frame(desc_pane, bg=WHITE)
        desc_wrap.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        self.desc_view = HebrewDescription(desc_wrap)
        self.desc_view.pack(fill="both", expand=True)

        footer = ttk.Frame(body)
        work_row = ttk.Frame(footer)
        work_row.pack(fill="x")
        work_row.columnconfigure(1, weight=1)
        self.work_label = ttk.Label(work_row, textvariable=self.work_hint, width=12)
        self.work_label.grid(row=0, column=0, sticky="w", padx=(0, 8))
        self._callout(self.work_label, "Working, Done, Stopped, or Failed, plus a count such as 12 / 150 while Search is running.")
        self.progress = ttk.Progressbar(work_row, mode="determinate", maximum=100, value=0)
        self.progress.grid(row=0, column=1, sticky="ew")
        self._callout(self.progress, "Fills while Search or a publisher lookup is running. Empty when SISU is idle.")
        self._progress_determinate = True
        self.scan_live_label = ttk.Label(footer, textvariable=self.scan_live, wraplength=1, justify="left")
        self.scan_live_label.pack(fill="x", pady=(4, 0))
        self._callout(
            self.scan_live_label,
            "The site being searched and how many books are already on the list.",
        )
        action_row = ttk.Frame(footer)
        action_row.pack(fill="x", pady=(4, 0))
        important_btn = ttk.Button(action_row, text="Important", command=self._select_important)
        important_btn.pack(side="left", pady=2)
        self._callout(
            important_btn,
            "Tick books that need attention: created and not passed to the database, updated after they were created, or updated after they were passed.",
        )
        self.status_label = ttk.Label(action_row, textvariable=self.status, wraplength=1, justify="left")
        self.status_label.pack(side="left", fill="x", expand=True, padx=16, pady=2)
        self._callout(self.status_label, "What SISU is doing right now, including progress, success, and errors.")
        self._callout(self.summary_label, "Summary of the last search: how many books are on the list, how many were new, and how many catalog pages were read.")
        footer.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        split.grid(row=3, column=0, sticky="nsew")
        self._bind_list_excel_path(rename_existing=False)
        self.list_title.trace_add("write", lambda *_args: self._on_list_fields_changed())
        self.year.trace_add("write", lambda *_args: self._on_list_fields_changed())
        self.max_pages.trace_add("write", lambda *_args: self._on_list_fields_changed())
        self.no_page_limit.trace_add("write", lambda *_args: self._on_list_fields_changed())
        self.include_unknown.trace_add("write", lambda *_args: self._on_list_fields_changed())
        body.bind("<Configure>", self._sync_full_width_wraps, add="+")
        self.list_status_label.bind(
            "<Configure>",
            lambda event: self._set_label_wrap(self.list_status_label, max(80, event.width - 4)),
            add="+",
        )
        footer.bind(
            "<Configure>",
            lambda event: (
                self._set_label_wrap(self.status_label, max(120, event.width - 120)),
                self._set_label_wrap(self.scan_live_label, max(120, event.width - 8)),
            ),
            add="+",
        )

    def _callout(self, widget: tk.Misc, text: str) -> None:
        HoverTip(widget, text)

    def _focus_is_text_field(self) -> bool:
        widget = self.focus_get()
        if widget is None:
            return False
        try:
            kind = str(widget.winfo_class())
        except tk.TclError:
            return False
        return kind in {"Entry", "TEntry", "Text", "TCombobox", "Combobox"}

    def _on_list_ctrl_home(self, _event=None) -> str | None:
        if self._focus_is_text_field():
            return None
        self.table.go_top()
        return "break"

    def _on_list_ctrl_end(self, _event=None) -> str | None:
        if self._focus_is_text_field():
            return None
        self.table.go_bottom()
        return "break"

    def _set_label_wrap(self, label: ttk.Label, width: int) -> None:
        width = max(40, int(width))
        try:
            current = int(float(label.cget("wraplength") or 0))
        except (TypeError, ValueError, tk.TclError):
            current = 0
        if current != width:
            label.configure(wraplength=width)

    def _sync_full_width_wraps(self, event: tk.Event) -> None:
        if event.widget is not getattr(self, "_layout_body", None):
            return
        width = max(40, int(event.width) - 8)
        self._set_label_wrap(self.colored_info_label, width)
        self._set_label_wrap(self.summary_label, width)

    def _on_url_text_modified(self, _event=None) -> None:
        url_box = getattr(self, "url_text", None)
        if url_box is None or not url_box.edit_modified():
            return
        url_box.edit_modified(False)
        self._fit_search_fields()
        self._on_list_fields_changed()

    def _fit_search_fields(self) -> None:
        url_box = getattr(self, "url_text", None)
        excel_box = getattr(self, "excel_entry", None)
        if url_box is None or excel_box is None:
            return
        url_font = tkfont.Font(font=url_box.cget("font"))
        lines = [line for line in url_box.get("1.0", "end-1c").splitlines() if line.strip()]
        if not lines:
            lines = ["https://www.booknet.co.il/"]
        url_cols = self._cols_for_texts(url_font, lines, 40, 68)
        if int(url_box.cget("width") or 0) != url_cols:
            url_box.configure(width=url_cols)
        path = self.excel_path.get().strip() or "C:\\SISU\\list.xlsx"
        excel_font = tkfont.nametofont("TkDefaultFont")
        excel_cols = self._cols_for_texts(excel_font, [path], 36, 64)
        if int(str(excel_box.cget("width") or 0)) != excel_cols:
            excel_box.configure(width=excel_cols)

    @staticmethod
    def _cols_for_texts(face: tkfont.Font, texts: list[str], min_cols: int, max_cols: int) -> int:
        zero = max(face.measure("0"), 1)
        widest = max(face.measure(text) for text in texts)
        cols = int((widest + zero * 2) / zero)
        return max(min_cols, min(max_cols, cols))

    def _urls(self) -> list[str]:
        return parse_site_urls(self.url_text.get("1.0", "end"))

    def _current_publishers(self) -> list[str]:
        from publisher_sites import _haystack

        names: list[str] = []
        seen: set[str] = set()
        for book in self.books:
            name = (book.publisher or "").strip()
            if not name:
                continue
            key = _haystack(name).strip()
            if not key or key in seen:
                continue
            seen.add(key)
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
        win.geometry("900x640")
        win.minsize(720, 520)
        self._settings_popup = win

        def close() -> None:
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
        llm_tab = ttk.Frame(body, padding=12)
        tab_frames = {
            "browser": browser_tab,
            "publishers": publisher_tab,
            "aliases": aliases_tab,
            "llm": llm_tab,
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
        make_tab("llm", "LLM")

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
        inner = _settings_scroll_table(table_wrap, columns=(1,))

        rows: list[tuple[tk.StringVar, tk.StringVar]] = []

        def _paint_url(shell: tk.Frame, var: tk.StringVar) -> None:
            empty = not var.get().strip()
            shell.configure(
                highlightbackground="#E2B93D" if empty else "#C9BBA8",
                highlightthickness=2 if empty else 1,
            )

        def _append_data_row(name: str = "", url: str = "", highlight: bool = False) -> None:
            name_var = tk.StringVar(value=name)
            url_var = tk.StringVar(value=url)
            grid_row = len(rows) + 1
            name_entry = tk.Entry(inner, textvariable=name_var, font=("Segoe UI", 10), relief="solid", bd=1, width=28)
            url_shell = tk.Frame(inner, bg=WHITE, highlightbackground="#C9BBA8", highlightthickness=1)
            url_entry = tk.Entry(url_shell, textvariable=url_var, font=("Segoe UI", 10), relief="flat", bd=0, bg=WHITE)
            url_entry.pack(fill="x", ipady=4, padx=2, pady=1)
            name_entry.grid(row=grid_row, column=0, sticky="ew", padx=(0, 6), pady=3, ipady=3)
            url_shell.grid(row=grid_row, column=1, sticky="ew", padx=(0, 6), pady=3)
            _bind_entry_clipboard(name_entry)
            _bind_entry_clipboard(url_entry)
            _paint_url(url_shell, url_var)
            url_var.trace_add("write", lambda *_args: _paint_url(url_shell, url_var))
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
            _suspend_settings_table(inner, True)
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
            _suspend_settings_table(inner, False)

        seed = merged_publisher_rows(self._current_publishers())
        if focus_publisher.strip() and not any(publishers_match(focus_publisher, name) for name, _url in seed):
            seed.insert(0, (focus_publisher.strip(), ""))

        ttk.Button(
            publisher_tab,
            text="Add publisher",
            command=lambda: _append_data_row("", "", highlight=True),
        ).pack(anchor="w")

        ttk.Label(
            aliases_tab,
            text="Page labels found on bookstore sites, and the catalog field each one should fill. Add a row, then Save.",
            wraplength=740,
        ).pack(anchor="w")
        alias_wrap = ttk.Frame(aliases_tab)
        alias_wrap.pack(fill="both", expand=True, pady=(8, 4))
        alias_inner = _settings_scroll_table(alias_wrap, columns=(0, 1))
        alias_rows: list[tuple[tk.StringVar, tk.StringVar]] = []
        field_choices = sorted(EXCEL_TARGETS.keys())

        def _append_alias_row(label: str = "", field: str = "") -> None:
            label_var = tk.StringVar(value=label)
            field_var = tk.StringVar(value=field)
            grid_row = len(alias_rows) + 1
            label_entry = tk.Entry(alias_inner, textvariable=label_var, font=("Segoe UI", 10), relief="solid", bd=1)
            field_combo = ttk.Combobox(
                alias_inner,
                textvariable=field_var,
                values=field_choices,
                font=("Segoe UI", 10),
            )
            label_entry.grid(row=grid_row, column=0, sticky="ew", padx=(0, 6), pady=3, ipady=3)
            field_combo.grid(row=grid_row, column=1, sticky="ew", padx=(0, 6), pady=3)
            _bind_entry_clipboard(label_entry)
            _bind_combobox_clipboard(field_combo)
            pair = (label_var, field_var)
            ttk.Button(
                alias_inner,
                text="Remove",
                command=lambda item=pair: _remove_alias_row(item),
            ).grid(row=grid_row, column=2, pady=3)
            alias_rows.append(pair)

        def _remove_alias_row(pair: tuple[tk.StringVar, tk.StringVar]) -> None:
            snapshot = [(label.get(), field.get()) for label, field in alias_rows if (label, field) != pair]
            _fill_alias_rows(snapshot)

        def _fill_alias_rows(items: list[tuple[str, str]]) -> None:
            _suspend_settings_table(alias_inner, True)
            for child in alias_inner.winfo_children():
                child.destroy()
            alias_rows.clear()
            ttk.Label(alias_inner, text="Page label", font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky="w")
            ttk.Label(alias_inner, text="Catalog field", font=("Segoe UI", 9, "bold")).grid(row=0, column=1, sticky="w")
            for label, field in items:
                _append_alias_row(label, field)
            _suspend_settings_table(alias_inner, False)

        alias_comment = ""
        cover_items: list[tuple[str, str]] = []
        loaded_aliases: list[tuple[str, str]] = []
        if ALIASES_PATH.exists():
            try:
                parsed = json.loads(ALIASES_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                parsed = {}
            if isinstance(parsed, dict):
                alias_comment = str(parsed.get("comment") or "")
                aliases_map = parsed.get("aliases") or {}
                if isinstance(aliases_map, dict):
                    loaded_aliases = sorted(
                        ((str(label), str(field)) for label, field in aliases_map.items() if str(label).strip()),
                        key=lambda item: (item[1].casefold(), item[0].casefold()),
                    )
                covers_map = parsed.get("cover_values") or {}
                if isinstance(covers_map, dict):
                    cover_items = [(str(word), str(code)) for word, code in covers_map.items()]
        ttk.Button(aliases_tab, text="Add label", command=lambda: _append_alias_row("", "")).pack(anchor="w")

        ttk.Label(
            aliases_tab,
            text="Cover type words (S = soft, H = hard, BB = board).",
            wraplength=740,
        ).pack(anchor="w", pady=(10, 0))
        cover_wrap = ttk.Frame(aliases_tab)
        cover_wrap.pack(fill="x", pady=(6, 4))
        cover_inner = _settings_scroll_table(cover_wrap, columns=(0,), height=140)
        cover_rows: list[tuple[tk.StringVar, tk.StringVar]] = []

        def _append_cover_row(word: str = "", code: str = "") -> None:
            word_var = tk.StringVar(value=word)
            code_var = tk.StringVar(value=code)
            grid_row = len(cover_rows) + 1
            word_entry = tk.Entry(cover_inner, textvariable=word_var, font=("Segoe UI", 10), relief="solid", bd=1)
            code_combo = ttk.Combobox(
                cover_inner,
                textvariable=code_var,
                values=("S", "H", "BB"),
                width=8,
                font=("Segoe UI", 10),
            )
            word_entry.grid(row=grid_row, column=0, sticky="ew", padx=(0, 6), pady=3, ipady=3)
            code_combo.grid(row=grid_row, column=1, sticky="w", padx=(0, 6), pady=3)
            _bind_entry_clipboard(word_entry)
            _bind_combobox_clipboard(code_combo)
            pair = (word_var, code_var)
            ttk.Button(
                cover_inner,
                text="Remove",
                command=lambda item=pair: _remove_cover_row(item),
            ).grid(row=grid_row, column=2, pady=3)
            cover_rows.append(pair)

        def _remove_cover_row(pair: tuple[tk.StringVar, tk.StringVar]) -> None:
            snapshot = [(word.get(), code.get()) for word, code in cover_rows if (word, code) != pair]
            _fill_cover_rows(snapshot)

        def _fill_cover_rows(items: list[tuple[str, str]]) -> None:
            _suspend_settings_table(cover_inner, True)
            for child in cover_inner.winfo_children():
                child.destroy()
            cover_rows.clear()
            ttk.Label(cover_inner, text="Word on the page", font=("Segoe UI", 9, "bold")).grid(
                row=0, column=0, sticky="w"
            )
            ttk.Label(cover_inner, text="Code", font=("Segoe UI", 9, "bold")).grid(row=0, column=1, sticky="w")
            for word, code in items:
                _append_cover_row(word, code)
            _suspend_settings_table(cover_inner, False)

        ttk.Button(aliases_tab, text="Add cover word", command=lambda: _append_cover_row("", "")).pack(anchor="w")

        llm = data.get("llm") if isinstance(data.get("llm"), dict) else {}
        llm_enabled = tk.BooleanVar(value=bool(llm.get("enabled")))
        service_var = tk.StringVar(value=str(llm.get("service") or "openai"))
        model_var = tk.StringVar(value=str(llm.get("model") or LLM_DEFAULT_MODELS.get("openai") or ""))
        api_key_var = tk.StringVar(value=str(llm.get("api_key") or ""))
        base_url_var = tk.StringVar(value=str(llm.get("base_url") or ""))
        show_key = tk.BooleanVar(value=False)
        no_token_limit = tk.BooleanVar(value=int(llm.get("token_limit") or 0) <= 0)
        token_limit_var = tk.StringVar(
            value="" if int(llm.get("token_limit") or 0) <= 0 else str(int(llm.get("token_limit") or 0))
        )
        usage_tokens = [max(0, int(llm.get("tokens_used") or 0))]
        usage_text = tk.StringVar(value="")

        ttk.Label(
            llm_tab,
            text="Allow an LLM only to generate phonetic English spellings of Hebrew titles — not translations. The key stays on this computer.",
            wraplength=740,
        ).pack(anchor="w")
        ttk.Checkbutton(llm_tab, text="Allow LLM", variable=llm_enabled).pack(anchor="w", pady=(10, 8))

        llm_form = ttk.Frame(llm_tab)
        llm_form.pack(fill="x")
        llm_form.columnconfigure(1, weight=1)
        service_labels = [label for _key, label in LLM_SERVICES]
        service_by_label = {label: key for key, label in LLM_SERVICES}
        label_by_service = {key: label for key, label in LLM_SERVICES}
        service_choice = tk.StringVar(value=label_by_service.get(service_var.get(), "OpenAI"))

        ttk.Label(llm_form, text="Service").grid(row=0, column=0, sticky="e", padx=(0, 8), pady=4)
        service_combo = ttk.Combobox(
            llm_form,
            textvariable=service_choice,
            values=service_labels,
            state="readonly",
            width=28,
        )
        service_combo.grid(row=0, column=1, sticky="w", pady=4)

        ttk.Label(llm_form, text="Model").grid(row=1, column=0, sticky="e", padx=(0, 8), pady=4)
        model_entry = ttk.Entry(llm_form, textvariable=model_var)
        model_entry.grid(row=1, column=1, sticky="ew", pady=4)
        model_hint = ttk.Label(llm_form, text="")
        model_hint.grid(row=2, column=1, sticky="w", pady=(0, 4))

        ttk.Label(llm_form, text="API key").grid(row=3, column=0, sticky="e", padx=(0, 8), pady=4)
        key_row = ttk.Frame(llm_form)
        key_row.grid(row=3, column=1, sticky="ew", pady=4)
        key_row.columnconfigure(0, weight=1)
        key_entry = ttk.Entry(key_row, textvariable=api_key_var, show="*")
        key_entry.grid(row=0, column=0, sticky="ew")
        ttk.Checkbutton(key_row, text="Show", variable=show_key).grid(row=0, column=1, padx=(8, 0))

        ttk.Label(llm_form, text="API base URL").grid(row=4, column=0, sticky="e", padx=(0, 8), pady=4)
        base_entry = ttk.Entry(llm_form, textvariable=base_url_var)
        base_entry.grid(row=4, column=1, sticky="ew", pady=4)
        ttk.Label(
            llm_form,
            text="Leave blank for the service default. Required for Custom (OpenAI-compatible) services.",
            wraplength=560,
        ).grid(row=5, column=1, sticky="w", pady=(0, 8))

        ttk.Label(llm_form, text="Token budget").grid(row=6, column=0, sticky="ne", padx=(0, 8), pady=4)
        budget = ttk.Frame(llm_form)
        budget.grid(row=6, column=1, sticky="w", pady=4)
        ttk.Checkbutton(budget, text="No limit (the service may still cap usage)", variable=no_token_limit).pack(
            anchor="w"
        )
        limit_row = ttk.Frame(budget)
        limit_row.pack(anchor="w", pady=(4, 0))
        ttk.Label(limit_row, text="Stop after").pack(side="left")
        token_limit_entry = ttk.Entry(limit_row, textvariable=token_limit_var, width=12)
        token_limit_entry.pack(side="left", padx=6)
        ttk.Label(limit_row, text="tokens (prompt + reply, counted from each response).").pack(side="left")
        _bind_entry_clipboard(model_entry)
        _bind_entry_clipboard(key_entry)
        _bind_entry_clipboard(base_entry)
        _bind_entry_clipboard(token_limit_entry)

        usage_label = ttk.Label(llm_tab, textvariable=usage_text, wraplength=740)
        usage_label.pack(anchor="w", pady=(12, 4))
        llm_buttons = ttk.Frame(llm_tab)
        llm_buttons.pack(anchor="w", pady=(4, 0))

        def _service_key() -> str:
            return service_by_label.get(service_choice.get(), "openai")

        def _refresh_usage_label() -> None:
            from llm_client import usage_sentence

            limit = 0 if no_token_limit.get() else _parse_token_limit(token_limit_var.get())
            preview = {
                "tokens_used": usage_tokens[0],
                "token_limit": limit,
                "last_total_tokens": int(llm.get("last_total_tokens") or 0),
                "last_error": str(llm.get("last_error") or ""),
            }
            usage_text.set(usage_sentence(preview))

        def _parse_token_limit(raw: str) -> int:
            text = (raw or "").strip().replace(",", "")
            if not text:
                return 0
            try:
                return max(0, int(text))
            except ValueError:
                return -1

        def _sync_key_show(*_args: object) -> None:
            key_entry.configure(show="" if show_key.get() else "*")

        def _sync_limit_state(*_args: object) -> None:
            token_limit_entry.configure(
                state="disabled" if (not llm_enabled.get() or no_token_limit.get()) else "normal"
            )
            _refresh_usage_label()

        def _sync_service(*_args: object) -> None:
            key = _service_key()
            previous = service_var.get()
            service_var.set(key)
            if key != previous:
                model_var.set(LLM_DEFAULT_MODELS.get(key, ""))
            default_model = LLM_DEFAULT_MODELS.get(key) or "set a model name"
            default_base = LLM_DEFAULT_BASE_URLS.get(key) or "your OpenAI-compatible URL"
            model_hint.configure(text=f"Default for this service: {default_model}. Base: {default_base}")

        def _llm_fields() -> dict:
            key = _service_key()
            limit = 0 if no_token_limit.get() else _parse_token_limit(token_limit_var.get())
            if limit < 0:
                raise ValueError("Token limit must be a whole number, or turn on No limit.")
            if llm_enabled.get() and not api_key_var.get().strip():
                raise ValueError("Paste an API key, or turn off Allow LLM.")
            if llm_enabled.get() and not model_var.get().strip():
                raise ValueError("Choose a model, or turn off Allow LLM.")
            if llm_enabled.get() and key == "custom" and not base_url_var.get().strip():
                raise ValueError("Custom LLM needs an API base URL.")
            return {
                "enabled": bool(llm_enabled.get()),
                "service": key,
                "api_key": api_key_var.get().strip(),
                "model": model_var.get().strip(),
                "base_url": base_url_var.get().strip().rstrip("/"),
                "token_limit": limit,
                "tokens_used": usage_tokens[0],
            }

        def _persist_llm_fields() -> dict:
            from app_config import update_llm_config

            fields = _llm_fields()
            return update_llm_config(**fields)

        def check_llm() -> None:
            try:
                _persist_llm_fields()
            except ValueError as exc:
                messagebox.showerror("LLM", str(exc))
                return
            win.configure(cursor="watch")
            win.update_idletasks()
            try:
                from llm_client import test_connection

                ok, message = test_connection()
            finally:
                win.configure(cursor="")
            latest = load_config().get("llm") or {}
            usage_tokens[0] = max(0, int(latest.get("tokens_used") or 0))
            llm["last_total_tokens"] = int(latest.get("last_total_tokens") or 0)
            llm["last_error"] = str(latest.get("last_error") or "")
            _refresh_usage_label()
            if ok:
                messagebox.showinfo("LLM", message)
            else:
                messagebox.showerror("LLM", message)

        def reset_usage() -> None:
            from app_config import update_llm_config

            usage_tokens[0] = 0
            update_llm_config(tokens_used=0, last_prompt_tokens=0, last_completion_tokens=0, last_total_tokens=0, warned_ratio=0)
            llm["last_total_tokens"] = 0
            llm["last_error"] = ""
            _refresh_usage_label()

        check_btn = ttk.Button(llm_buttons, text="Check connection", command=check_llm)
        check_btn.pack(side="left")
        reset_btn = ttk.Button(llm_buttons, text="Reset usage count", command=reset_usage)
        reset_btn.pack(side="left", padx=(8, 0))

        def _sync_llm_enabled(*_args: object) -> None:
            on = bool(llm_enabled.get())
            state = "normal" if on else "disabled"
            combo_state = "readonly" if on else "disabled"
            service_combo.configure(state=combo_state)
            for widget in (model_entry, key_entry, base_entry):
                widget.configure(state=state)
            token_limit_entry.configure(
                state="disabled" if (not on or no_token_limit.get()) else "normal"
            )
            check_btn.configure(state=state)
            reset_btn.configure(state=state)

        show_key.trace_add("write", _sync_key_show)
        no_token_limit.trace_add("write", _sync_limit_state)
        llm_enabled.trace_add("write", _sync_llm_enabled)
        service_combo.bind("<<ComboboxSelected>>", lambda _e: _sync_service())
        _sync_service()
        _sync_key_show()
        _sync_limit_state()
        _sync_llm_enabled()

        def save_aliases() -> bool:
            aliases: dict[str, str] = {}
            for label_var, field_var in alias_rows:
                label = label_var.get().strip()
                field = field_var.get().strip()
                if not label:
                    continue
                if not field:
                    messagebox.showerror("Field aliases", f"Choose a catalog field for “{label}”.")
                    return False
                aliases[label] = field
            covers: dict[str, str] = {}
            for word_var, code_var in cover_rows:
                word = word_var.get().strip()
                code = code_var.get().strip().upper()
                if not word:
                    continue
                if code not in {"S", "H", "BB"}:
                    messagebox.showerror("Field aliases", f"Cover code for “{word}” must be S, H, or BB.")
                    return False
                covers[word] = code
            payload = {
                "comment": alias_comment
                or "Maps labels found on bookstore and publisher pages to SISU field keys.",
                "aliases": aliases,
                "cover_values": covers,
            }
            ALIASES_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            reload_aliases()
            self._set_status("Field aliases saved. The next Search or More will use them.")
            return True

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(
            buttons,
            text="Check for updates",
            command=lambda: self.check_for_updates(silent=False),
        ).pack(side="left")

        def save() -> None:
            publishers: dict[str, str] = {}
            for name_var, url_var in rows:
                name = name_var.get().strip()
                if not name:
                    continue
                publishers[name] = normalize_site_url(url_var.get())
            try:
                llm_fields = _llm_fields()
            except ValueError as exc:
                show_tab("llm")
                messagebox.showerror("LLM", str(exc))
                return
            current = load_config()
            current_llm = dict(current.get("llm") or {})
            current_llm.update(llm_fields)
            save_config(
                {
                    "browser": browser_var.get().strip() or "chrome",
                    "browser_path": custom_path.get().strip(),
                    "publishers": publishers,
                    "excel_dir": current.get("excel_dir") or "",
                    "llm": current_llm,
                }
            )
            if not save_aliases():
                show_tab("aliases")
                return
            close()
            extra = ""
            if llm_fields.get("enabled") and self.books:
                extra = self._refill_phonetics_with_llm()
            self._set_status(("Settings saved." + (" " + extra if extra else "")).strip())
            if self._selected_book:
                self.show_book(self._selected_book)

        ttk.Button(buttons, text="Cancel", command=close).pack(side="right")
        ttk.Button(buttons, text="Save", command=save, style="Accent.TButton").pack(side="right", padx=(0, 8))
        start_tab = (
            "llm"
            if focus_tab == "llm"
            else "aliases"
            if focus_tab == "aliases"
            else "publishers"
            if focus_publisher.strip()
            else "browser"
        )
        show_tab(start_tab)
        win.lift()
        win.focus_force()
        win.update_idletasks()
        _fill_rows(seed, highlight_name=focus_publisher)
        _fill_alias_rows(loaded_aliases)
        _fill_cover_rows(cover_items)

    def _on_list_title_change(self) -> None:
        self._bind_list_excel_path(rename_existing=not self._list_locked)
        self._persist_working()

    def _default_excel_dir(self) -> Path:
        configured = str(load_config().get("excel_dir") or "").strip()
        path = Path(configured) if configured else APP_DIR
        return path if path.exists() else APP_DIR

    def _list_excel_path(self) -> Path:
        folder = Path(self._excel_dir or self._default_excel_dir())
        return folder / list_excel_filename(self.list_title.get().strip() or "New", self._list_id)

    def _bind_list_excel_path(self, *, rename_existing: bool = True) -> None:
        new_path = self._list_excel_path()
        old_raw = self.excel_path.get().strip()
        old_path = Path(old_raw) if old_raw else None
        if (
            rename_existing
            and old_path
            and old_path.exists()
            and old_path.resolve() != new_path.resolve()
            and not new_path.exists()
        ):
            try:
                old_path.rename(new_path)
            except OSError:
                pass
        self.excel_path.set(str(new_path))

    def _ensure_list_excel(self, *, create: bool = True) -> Path | None:
        self._bind_list_excel_path(rename_existing=False)
        path = Path(self.excel_path.get().strip())
        if path.exists():
            return path
        if not create:
            return None
        try:
            return ensure_list_workbook(path, SCHEMA_EXCEL)
        except Exception as exc:
            messagebox.showerror("Excel", str(exc))
            return None

    def browse_excel_folder(self) -> None:
        if self._guard_locked("change this list's Excel folder"):
            return
        chosen = filedialog.askdirectory(
            title="Folder for this list's Excel file",
            initialdir=str(self._excel_dir if Path(self._excel_dir).exists() else APP_DIR),
        )
        if not chosen:
            return
        new_dir = Path(chosen)
        old_path = Path(self.excel_path.get().strip()) if self.excel_path.get().strip() else None
        self._excel_dir = new_dir
        data = load_config()
        data["excel_dir"] = str(new_dir)
        save_config(data)
        self._bind_list_excel_path(rename_existing=False)
        new_path = Path(self.excel_path.get().strip())
        if old_path and old_path.exists() and old_path.resolve() != new_path.resolve():
            new_path.parent.mkdir(parents=True, exist_ok=True)
            if not new_path.exists():
                try:
                    shutil.move(str(old_path), str(new_path))
                except OSError as exc:
                    messagebox.showerror("Excel", f"Could not move the Excel file:\n{exc}")
        self.refresh_excel_info()
        self._sync_books_from_list_excel(self.books)
        self.table.set_books(self.books, keep_checks=True)
        self._persist_working()
        self._set_status(f"This list's Excel folder is {new_dir}")

    def open_excel(self) -> None:
        path = self._ensure_list_excel(create=not self._list_locked)
        if path is None or not path.exists():
            messagebox.showerror("Excel", "The Excel file for this list was not found.")
            return
        try:
            os.startfile(str(path))
            self._set_status(f"Opened {path.name}")
        except OSError as exc:
            messagebox.showerror("Could not open Excel", str(exc))

    def share_excel(self) -> None:
        path = self._ensure_list_excel(create=not self._list_locked)
        if path is None or not path.exists():
            messagebox.showerror("Share", "There is no Excel file for this list yet.")
            return
        title = self.list_title.get().strip() or "SISU list"
        subject = f"SISU catalog: {title}"
        body = (
            f"Please process the attached Excel catalog for scan list \"{title}\".\n\n"
            "After you take this file, lock the list in SISU so the spreadsheet is not changed."
        )
        if self._share_via_outlook(path, subject, body):
            self._set_status(f"Opened an email with {path.name} attached.")
            return
        if self._share_via_eml(path, subject, body):
            self._set_status(f"Opened an email draft with {path.name} attached.")
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(str(path))
        except tk.TclError:
            pass
        mailto = "mailto:?subject=" + quote(subject) + "&body=" + quote(body + "\n\n" + str(path))
        webbrowser.open(mailto)
        messagebox.showinfo(
            "Share Excel",
            "Could not attach the file in your mail program automatically.\n\n"
            f"The Excel path was copied to the clipboard:\n{path}",
        )

    def _share_via_outlook(self, path: Path, subject: str, body: str) -> bool:
        script = Path(tempfile.gettempdir()) / "sisu_share_excel.ps1"
        def ps_str(value: str) -> str:
            return "'" + value.replace("'", "''") + "'"

        script.write_text(
            "\n".join(
                [
                    "$ErrorActionPreference = 'Stop'",
                    "$outlook = New-Object -ComObject Outlook.Application",
                    "$mail = $outlook.CreateItem(0)",
                    f"$mail.Subject = {ps_str(subject)}",
                    f"$mail.Body = {ps_str(body)}",
                    f"$null = $mail.Attachments.Add({ps_str(str(path))})",
                    "$mail.Display() | Out-Null",
                ]
            ),
            encoding="utf-8-sig",
        )
        try:
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                capture_output=True,
                text=True,
                timeout=40,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0

    def _share_via_eml(self, path: Path, subject: str, body: str) -> bool:
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["Subject"] = subject
        msg.set_content(body)
        msg.add_attachment(
            path.read_bytes(),
            maintype="application",
            subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=path.name,
        )
        draft = APP_DIR / "cache" / "sisu_share_draft.eml"
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_bytes(msg.as_bytes())
        try:
            os.startfile(str(draft))
            return True
        except OSError:
            return False

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
            self.colored_info.set(
                f"This list’s Excel is {path.name} in {path.parent}. Open, Share, or Mark final will create it from the schema."
            )
            self.excel_columns = []
            self.excel_all_columns = []
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

    def _current_payload(self, report: dict | None = None) -> dict:
        return build_payload(
            books=self.books,
            urls=self._urls(),
            year=self.year.get().strip(),
            title=self.list_title.get().strip() or "New",
            list_id=self._list_id,
            locked=self._list_locked,
            archived=self._list_archived,
            max_pages=self._page_limit_setting(),
            include_unknown=bool(self.include_unknown.get()),
            report=report if report is not None else self._list_report,
            notes=self._list_notes,
            created_at=self._list_created_at,
            skip_cache_restore=self._skip_cache_restore,
            excel_dir=str(self._excel_dir or ""),
        )

    def _persist_working(self, report: dict | None = None) -> None:
        if report is not None:
            self._list_report = report
        save_working(self._current_payload())
        self._refresh_list_status()

    @staticmethod
    def _is_placeholder_title(title: str) -> bool:
        return (title or "").strip().casefold() in {"", "new", "untitled"}

    def _list_has_name(self) -> bool:
        if str(self._list_id or "").strip():
            return True
        return not self._is_placeholder_title(self.list_title.get())

    def _fingerprint_payload(self, data: dict | None) -> str:
        payload = dict(data or {})
        books = payload.get("books") or []
        slim_books = []
        for item in books:
            if not isinstance(item, dict):
                continue
            slim_books.append(
                {key: item.get(key) for key in sorted(item) if key not in {"extra"}}
            )
        body = {
            "title": str(payload.get("title") or "").strip(),
            "urls": list(payload.get("urls") or []),
            "year": str(payload.get("year") or "").strip(),
            "max_pages": int(payload.get("max_pages") or 0),
            "include_unknown": bool(payload.get("include_unknown")),
            "notes": str(payload.get("notes") or ""),
            "locked": bool(payload.get("locked")),
            "archived": bool(payload.get("archived")),
            "books": slim_books,
        }
        return json.dumps(body, ensure_ascii=False, sort_keys=True, default=str)

    def _list_fingerprint(self) -> str:
        try:
            return self._fingerprint_payload(self._current_payload())
        except tk.TclError:
            return ""

    def _mark_list_clean(self) -> None:
        self._clean_fingerprint = self._list_fingerprint()

    def _list_is_dirty(self) -> bool:
        return self._list_fingerprint() != self._clean_fingerprint

    def _on_list_fields_changed(self) -> None:
        if not getattr(self, "_list_actions_ready", False):
            return
        self._refresh_list_status()

    def _update_list_action_buttons(self) -> None:
        if not getattr(self, "save_btn", None):
            return
        named = self._list_has_name()
        dirty = self._list_is_dirty()
        can_save = dirty and not self._list_locked and not self._busy
        can_stash = (not named) and dirty and not self._list_locked and not self._busy
        can_restore = stash_has_data() and not self._busy
        self.save_btn.configure(
            state="normal" if can_save else "disabled",
            style="Accent.TButton" if can_save else "TButton",
        )
        self.stash_btn.configure(state="normal" if can_stash else "disabled")
        self.restore_btn.configure(state="normal" if can_restore else "disabled")

    def _refresh_list_status(self) -> None:
        dirty = self._list_is_dirty() if getattr(self, "_list_actions_ready", False) else False
        if self._list_locked:
            kind = "Locked list"
        elif self._list_id:
            kind = "Saved list · unsaved changes" if dirty else "Saved list"
        elif self.books:
            kind = "Working list · unsaved"
        else:
            kind = "Working list · new"
        extra = []
        if self._list_archived:
            extra.append("archived")
        if stash_has_data():
            extra.append("stash ready")
        suffix = f" · {', '.join(extra)}" if extra else ""
        self.list_status.set(f"{kind} · {len(self.books)} book(s){suffix}")
        self._apply_lock_state()
        self._update_list_action_buttons()

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
        self.no_limit_check.configure(state=edit_state)
        self.unknown_check.configure(state=edit_state)
        self._sync_page_limit_state()
        self.list_title_entry.configure(state=edit_state)
        folder_state = "disabled" if self._list_locked else "normal"
        self.excel_folder_btn.configure(state=folder_state)
        if self._list_locked:
            self.more_btn.configure(state="disabled")
            self.approve_btn.configure(state="disabled")
            self.final_btn.configure(state="disabled")
            self.unfinal_btn.configure(state="disabled")
        elif self._selected_book and not self._busy:
            self.more_btn.configure(state="normal")
            self._update_workflow_buttons(self._selected_book)
        self._update_list_action_buttons()

    def _apply_payload(self, data: dict, *, status: str = "", as_saved: bool | None = None) -> None:
        self._list_id = str(data.get("id") or "")
        self._list_locked = bool(data.get("locked"))
        self._list_archived = bool(data.get("archived"))
        self._list_created_at = str(data.get("created_at") or "")
        self._list_notes = str(data.get("notes") or "")
        self._list_report = data.get("report") or {}
        self._skip_cache_restore = bool(data.get("skip_cache_restore"))
        self.list_title.set(str(data.get("title") or "New"))
        urls = merge_search_urls(list(data.get("urls") or []))
        self.url_text.configure(state="normal")
        self.url_text.delete("1.0", "end")
        self.url_text.insert("1.0", "\n".join(urls))
        if data.get("year") not in (None, ""):
            self.year.set(str(data.get("year")))
        if "max_pages" in data and data.get("max_pages") is not None:
            try:
                value = int(data.get("max_pages"))
            except (TypeError, ValueError):
                value = 40
            if value <= 0:
                self.no_page_limit.set(True)
            else:
                self.no_page_limit.set(False)
                self.max_pages.set(value)
            self._set_pages_total(0)
            self._sync_page_limit_state()
        if "include_unknown" in data:
            self.include_unknown.set(bool(data.get("include_unknown")))
        excel_dir = str(data.get("excel_dir") or "").strip()
        self._excel_dir = Path(excel_dir) if excel_dir else self._default_excel_dir()
        self._bind_list_excel_path(rename_existing=False)
        self.books = books_from_payload(data)
        cleanup = dedupe_book_list(self.books)
        phonetic_filled = self._prepare_books(self.books)
        self.table.set_books(self.books, keep_checks=False)
        self._clear_selected_book()
        self.refresh_excel_info()
        self._persist_working()
        if as_saved is True:
            self._mark_list_clean()
        elif as_saved is False:
            self._clean_fingerprint = ""
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
        message = status or f"Opened “{self.list_title.get()}” with {len(self.books)} book(s)."
        extra = self._phonetic_fill_message(phonetic_filled)
        if extra:
            message = f"{message} {extra}"
        if cleanup.removed:
            message = f"{message} {cleanup.summary()}"
            self.after(400, lambda report=cleanup: self._show_stats_report("Duplicate cleanup", report.report_text()))
        self._set_status(message)
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
        if self._list_id:
            named = load_named(self._list_id)
            self._clean_fingerprint = self._fingerprint_payload(named) if named else ""
        elif self.books or not self._is_placeholder_title(self.list_title.get()):
            self._clean_fingerprint = ""
        else:
            self._mark_list_clean()
        self._refresh_list_status()

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
            max_pages=self._page_limit_setting(),
            include_unknown=bool(self.include_unknown.get()),
        )
        payload["excel_dir"] = str(self._excel_dir or "")
        self._apply_payload(payload, status="Started a new empty working list.", as_saved=True)

    def stash_working_list(self) -> None:
        if self._busy:
            return
        if self._list_has_name():
            messagebox.showinfo(
                "Stash",
                "This list already has a name. Save it instead of stashing. Stash is only for nameless working lists.",
            )
            return
        if not self._list_is_dirty() and not self.books:
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
            max_pages=self._page_limit_setting(),
            include_unknown=bool(self.include_unknown.get()),
        )
        payload["excel_dir"] = str(self._excel_dir or "")
        self._apply_payload(
            payload,
            status="Stashed the previous list. This working list is empty for a new search.",
            as_saved=True,
        )

    def restore_stash(self) -> None:
        if self._busy:
            return
        data = load_stash()
        if not data or not stash_has_data():
            messagebox.showinfo("Stash", "There is no stashed list to restore.")
            return
        if self.books and not messagebox.askyesno(
            "Restore stash",
            f"Replace the current working list with the stash?\n\n{stash_summary()}",
        ):
            return
        self._apply_payload(data, status=f"Restored stash: {stash_summary()}.", as_saved=False)

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
        self._bind_list_excel_path(rename_existing=True)
        self._persist_working()
        self._mark_list_clean()
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
        btns = ttk.Frame(body)
        btns.pack(side="bottom", fill="x", pady=(10, 0))
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
        scroll.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)
        self._lists_tree = tree
        tree.bind("<Double-1>", lambda _e: self._open_selected_saved_list())
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
        self._apply_payload(data, as_saved=True)
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
            self._bind_list_excel_path(rename_existing=not self._list_locked)
            self._persist_working()
            self._mark_list_clean()
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
            self._mark_list_clean()
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
            self._clean_fingerprint = ""
            self._refresh_list_status()
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
        self.unfinal_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._update_list_action_buttons()
        self._cancel_live_save()
        seed_books = list(self.books)
        scan_started = entry_now()
        fill_missing_entry_dates(self.books, scan_started)
        fill_missing_phonetics(self.books, use_llm=False)
        if self.books:
            self.table.set_books(self.books, keep_checks=True)
        self._follow_search = True
        self._scan_site = ""
        self._scan_found = len(self.books)
        self._scan_checking = 0
        self._scan_total = 0
        self._scan_phase = "Listing"
        self._scan_book = ""
        self.scan_live.set("")
        self._set_pages_total(0)
        self._highlight_site_url("")
        self._begin_work("Working…")
        self._set_status(
            "Starting search. Each catalog URL is listed once, in order. "
            "Duplicates merge into one row. Created and updated dates are kept."
        )
        self.summary.set("Search running — listing each catalog once. Product pages are not opened.")
        pages = self._page_limit_setting()
        self._activity.start_run(
            "Search",
            f"Year {year or 'any'}. {len(urls)} catalog URL(s). "
            f"{'No page limit' if pages <= 0 else f'Max {pages} listing pages per site'}. "
            f"{len(seed_books)} book(s) already on the list.",
        )
        thread = threading.Thread(
            target=self._run_crawl,
            args=(
                urls,
                year,
                self._page_limit_setting(),
                bool(self.include_unknown.get()),
                self.list_title.get().strip(),
                self._list_id,
                self._list_locked,
                self._list_archived,
                self._list_notes,
                self._list_created_at,
                seed_books,
                scan_started,
            ),
            daemon=True,
        )
        thread.start()

    def stop_search(self) -> None:
        self._cancel.set()
        self._set_status("Stopping…")

    def _crawl_progress(self, msg: str) -> None:
        try:
            self._activity.log(msg)
        except Exception:
            pass
        self._ui_queue.put(("status", msg))

    def show_activity_log(self) -> None:
        if self._log_popup is not None:
            try:
                self._log_popup.lift()
                self._log_popup.focus_force()
                self._reload_log_list(keep_selection=True)
                return
            except tk.TclError:
                self._log_popup = None
        win = tk.Toplevel(self)
        win.title("Search log")
        win.geometry("980x560")
        win.minsize(720, 400)
        self._log_popup = win
        body = ttk.Frame(win, padding=10)
        body.pack(fill="both", expand=True)
        ttk.Label(
            body,
            text="Each Search is a separate log. Progress lines such as listing book 48 are rewritten in place so the file stays small. The current search is saved every few seconds while it runs.",
            wraplength=940,
        ).pack(anchor="w", pady=(0, 8))
        split = ttk.Panedwindow(body, orient="horizontal")
        split.pack(fill="both", expand=True)
        left = ttk.Frame(split)
        right = ttk.Frame(split)
        split.add(left, weight=1)
        split.add(right, weight=3)
        ttk.Label(left, text="Executions").pack(anchor="w")
        listbox = tk.Listbox(left, font=("Segoe UI", 10), exportselection=False)
        list_scroll = ttk.Scrollbar(left, orient="vertical", command=listbox.yview)
        listbox.configure(yscrollcommand=list_scroll.set)
        listbox.pack(side="left", fill="both", expand=True)
        list_scroll.pack(side="right", fill="y")
        self._log_list = listbox
        ttk.Label(right, text="Log").pack(anchor="w")
        view = tk.Text(right, font=("Consolas", 10), wrap="word", state="disabled")
        text_scroll = ttk.Scrollbar(right, orient="vertical", command=view.yview)
        view.configure(yscrollcommand=text_scroll.set)
        view.pack(side="left", fill="both", expand=True)
        text_scroll.pack(side="right", fill="y")
        self._log_view = view

        def on_select(_event=None) -> None:
            selection = listbox.curselection()
            if not selection:
                return
            index = int(selection[0])
            if 0 <= index < len(self._log_runs):
                self._log_selected_id = self._log_runs[index]["id"]
                self._show_selected_log()

        listbox.bind("<<ListboxSelect>>", on_select)

        def close() -> None:
            self._stop_log_refresh()
            self._log_popup = None
            self._log_list = None
            self._log_view = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", close)
        ttk.Button(body, text="Close", command=close).pack(anchor="e", pady=(8, 0))
        self._reload_log_list()
        self._start_log_refresh()

    def _reload_log_list(self, *, keep_selection: bool = False) -> None:
        box = self._log_list
        if box is None:
            return
        previous = self._log_selected_id if keep_selection else ""
        self._log_runs = self._activity.list_runs()
        box.delete(0, "end")
        chosen = 0
        for index, item in enumerate(self._log_runs):
            box.insert("end", item["label"])
            if item["id"] == previous or (not previous and index == 0):
                chosen = index
        if self._log_runs:
            box.selection_clear(0, "end")
            box.selection_set(chosen)
            box.see(chosen)
            self._log_selected_id = self._log_runs[chosen]["id"]
            self._show_selected_log()
        else:
            self._set_log_view("No search logs yet. Run Search to create the first log.")

    def _show_selected_log(self) -> None:
        run_id = self._log_selected_id
        if not run_id:
            self._set_log_view("No search logs yet.")
            return
        at_end = False
        view = self._log_view
        if view is not None:
            try:
                at_end = float(view.yview()[1]) >= 0.98
            except tk.TclError:
                at_end = True
        self._set_log_view(self._activity.render_run(run_id), stick_to_end=at_end or run_id == self._activity.current_id())

    def _set_log_view(self, text: str, *, stick_to_end: bool = True) -> None:
        view = self._log_view
        if view is None:
            return
        view.configure(state="normal")
        view.delete("1.0", "end")
        view.insert("1.0", text)
        view.configure(state="disabled")
        if stick_to_end:
            view.see("end")

    def _start_log_refresh(self) -> None:
        self._stop_log_refresh()
        self._log_refresh_job = self.after(400, self._refresh_log_window)

    def _stop_log_refresh(self) -> None:
        job = self._log_refresh_job
        self._log_refresh_job = None
        if job:
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass

    def _refresh_log_window(self) -> None:
        self._log_refresh_job = None
        if self._log_popup is None:
            return
        try:
            if not self._log_popup.winfo_exists():
                self._log_popup = None
                return
        except tk.TclError:
            self._log_popup = None
            return
        current = self._activity.current_id()
        if self._log_selected_id == current or not self._log_selected_id:
            if self._log_runs and self._log_runs[0]["id"] != current:
                self._reload_log_list(keep_selection=True)
            else:
                self._show_selected_log()
        self._log_refresh_job = self.after(400, self._refresh_log_window)

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
        seed_books: list[Book] | None = None,
        seed_stamp: str = "",
    ) -> None:
        crawler = BookCrawler(
            cancelled=self._cancel.is_set,
            progress=self._crawl_progress,
            event=lambda kind, data: self._ui_queue.put(("event", (kind, data))),
        )
        try:
            self._crawl_progress(f"Searching {len(urls)} bookstore and catalog URL(s)…")
            books = list(seed_books or [])
            books = crawler.search_all_sites(
                urls=urls,
                year=year,
                max_listing_pages=max_pages,
                include_unknown_year=include_unknown,
                seed_books=books,
                seed_stamp=seed_stamp,
            )
            self._prepare_books(books)
            try:
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
                        excel_dir=str(self._excel_dir or ""),
                    )
                )
            except OSError:
                pass
            try:
                write_field_report(self.excel_path.get().strip() or None)
            except Exception:
                pass
            self._ui_queue.put(("done", (books, crawler.report)))
        except CrawlCancelled:
            self._ui_queue.put(("cancelled", (books, crawler.report)))
        except Exception as exc:
            self._ui_queue.put(("error", str(exc)))
        finally:
            try:
                from book_cache import flush_page_cache

                flush_page_cache()
            except Exception:
                pass

    def _drain_queue(self) -> None:
        processed = 0
        while processed < 8:
            try:
                kind, payload = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            processed += 1
            if kind == "status":
                self._set_status(str(payload))
                if self._lookup_popup is not None:
                    self._lookup_status.set(str(payload))
                    self._append_lookup_log(str(payload))
            elif kind == "event":
                event_kind, data = payload
                self._on_search_event(str(event_kind), data or {})
            elif kind == "lookup_step":
                self._set_lookup_step(payload)
            elif kind == "done":
                books, report = payload
                self._finish_search(books, cancelled=False, report=report)
            elif kind == "cancelled":
                if isinstance(payload, tuple) and len(payload) == 2:
                    books, report = payload
                    self._finish_search(books, cancelled=True, report=report)
                else:
                    self._finish_search(self.books, cancelled=True)
            elif kind == "error":
                self._finish_search(self.books, cancelled=False, failed=True, error=str(payload))
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
        self.after(20 if processed >= 8 else 80, self._drain_queue)

    def _finish_search(
        self,
        books: list[Book],
        cancelled: bool,
        report: CrawlReport | None = None,
        failed: bool = False,
        error: str = "",
    ) -> None:
        self._busy = False
        self.stop_btn.configure(state="disabled")
        self._follow_search = True
        self._highlight_site_url("")
        self._cancel_live_save()
        if not failed:
            self.scan_live.set("")
        phonetic_note = ""
        if books:
            self.books = books
            filled = self._prepare_books(self.books)
            phonetic_note = self._phonetic_fill_message(filled)
            self.table.set_books(self.books, keep_checks=False)
        if (not self.list_title.get().strip() or self.list_title.get().strip() == "New") and self.books:
            self.list_title.set(default_scan_title(len(self.books), self.year.get().strip()))
        if self._selected_book not in self.books:
            self._clear_selected_book()
        elif self._selected_book:
            self.show_book(self._selected_book)
        if report is None:
            report = CrawlReport(matched=len(self.books), cancelled=cancelled)
        report.error_books = sum(1 for book in self.books if (book.scan_status or "") == "failed")
        report.matched = max(int(report.matched or 0), len(self.books))
        self._list_report = asdict(report)
        self._persist_working(self._list_report)
        year = self.year.get().strip() or "any year"
        list_failed = failed or bool(report.error and not self.books and not cancelled)
        dup_note = self._scan_duplicate_status(report)

        def _status(text: str) -> None:
            extra = " ".join(part for part in (phonetic_note, dup_note) if part)
            self._set_status((text + (" " + extra if extra else "")).strip())

        if cancelled:
            self._end_work("stopped")
            _status(f"Stopped. {len(self.books)} book(s) from {year} are kept in the working list.")
        elif list_failed:
            self._end_work("failed")
            detail = (error or report.error or "See the error message.").strip()
            _status(f"Search failed. {detail}")
        else:
            self._end_work("done")
            _status(
                f"Search finished. {len(self.books)} book(s) from {year}. "
                "The working list is kept until you save or clear it."
            )
        outcome = "stopped" if cancelled else "failed" if list_failed else "done"
        self._activity.finish(outcome, self.status.get())
        self.summary.set(report.summary())
        if dup_note:
            self.after(400, lambda r=report: self._show_stats_report("Search duplicates", self._scan_duplicate_report(r)))
        if self._selected_book:
            self.more_btn.configure(state="normal")
            self._update_workflow_buttons(self._selected_book)
        self._refresh_list_status()
        self._refresh_selection_label()

    def _scan_duplicate_status(self, report: CrawlReport) -> str:
        found = int(report.duplicates_found or 0)
        updated = int(report.duplicates_updated or 0)
        removed = int(report.list_removed or 0)
        modified = int(report.list_modified or 0)
        parts: list[str] = []
        if found:
            parts.append(
                f"{found:,} existing book(s) were duplicates and were not added again"
                + (f"; {updated:,} of them gained fields from later listings" if updated else "")
                + "."
            )
        if removed:
            parts.append(
                f"Cleared {removed:,} extra duplicate row(s) from the list"
                + (f"; {modified:,} earlier book(s) were updated from those later rows" if modified else "")
                + "."
            )
        return " ".join(parts)

    def _scan_duplicate_report(self, report: CrawlReport) -> str:
        lines = [
            "Search duplicate report",
            "",
            f"Books on the list now: {len(self.books):,}",
            f"New books this search: {int(report.new_names or 0):,}",
            f"Existing books discovered as duplicates (not added again): {int(report.duplicates_found or 0):,}",
            f"Existing books updated from later listings: {int(report.duplicates_updated or 0):,}",
        ]
        if report.list_removed:
            lines.extend(
                [
                    "",
                    "Leftover rows still on the list were cleaned the same way:",
                    f"Books that had extra rows: {int(report.list_duplicate_books or 0):,}",
                    f"Extra rows merged and removed: {int(report.list_removed or 0):,}",
                    f"Earlier books updated from those later rows: {int(report.list_modified or 0):,}",
                ]
            )
        return "\n".join(lines)

    def _show_stats_report(self, title: str, body: str) -> None:
        win = tk.Toplevel(self)
        win.title(title)
        win.geometry("560x380")
        win.minsize(420, 260)
        win.transient(self)
        frame = ttk.Frame(win, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        view = tk.Text(frame, wrap="word", font=("Segoe UI", 10), padx=8, pady=8)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=view.yview)
        view.configure(yscrollcommand=scroll.set)
        view.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        view.insert("1.0", body)
        view.configure(state="disabled")
        ttk.Button(frame, text="OK", command=win.destroy).grid(row=1, column=0, columnspan=2, sticky="e", pady=(10, 0))
        win.lift()
        win.focus_force()

    def _on_check_change(self) -> None:
        self._refresh_selection_label()
        if self._busy:
            return
        total, shown, selected, selected_shown = self.table.view_counts()
        if self.table.filter_keys:
            self._set_status(
                f"{selected:,} selected · {shown:,} shown of {total:,}"
                + (f" · {selected_shown:,} selected in this view" if selected and selected_shown != selected else "")
            )
        else:
            self._set_status(f"{selected:,} selected of {total:,}")

    def _refresh_selection_label(self) -> None:
        total, shown, selected, selected_shown = self.table.view_counts()
        text = f"{selected:,} selected · {shown:,} shown · {total:,} total"
        if self.table.filter_keys and selected and selected_shown != selected:
            text = f"{selected:,} selected ({selected_shown:,} in this view) · {shown:,} shown · {total:,} total"
        self.table_counts.set(text)

    def _set_running_summary(self) -> None:
        if not self._busy:
            return
        total = len(self.books)
        self.summary.set(
            f"Search running — {total:,} book(s) on the list. "
            "Each catalog is listed once; duplicates merge into one row."
        )

    def _select_important(self) -> None:
        count = self.table.select_important()
        if count:
            self._set_status(
                f"Selected {count} important book(s): created and not passed, "
                "updated after created, or updated after the last database pass."
            )
        else:
            self._set_status("No important books. Everything on this list is already passed to the database.")

    def show_book(self, book: Book) -> None:
        if self._busy and not self._programmatic_select:
            self._follow_search = False
        self._selected_book = book
        if not self._busy:
            self.more_btn.configure(state="normal")
        self._update_workflow_buttons(book)
        book.refresh_text_fields()
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
        self.unfinal_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._update_list_action_buttons()
        self._begin_work("Working…")
        label = books[0].publisher.strip() if books else "publisher"
        site = resolve_publisher_site(label) or ""
        self._activity.start_run(
            "More details",
            f"Publisher lookup on {label} for {len(books)} book(s).",
        )
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

    def _complete_lookup_popup(self, summary: str, failed: bool = False) -> None:
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
        self._lookup_title.set("Lookup failed" if failed else "Lookup finished")
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
            progress=self._crawl_progress,
        )
        remap: dict[str, str] = {}
        updated = 0
        errors = 0
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
                if book.extra.get("lookup_error"):
                    errors += 1
                elif filled:
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
                        "errors": errors,
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
            markdown = report.read_text(encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("Field report", str(exc))
            return
        self._set_status("Field matching report is open.")
        existing = self._report_popup
        view = self._report_view
        if existing is not None and view is not None:
            try:
                if existing.winfo_exists():
                    _fill_markdown_view(view, markdown)
                    existing.lift()
                    existing.focus_force()
                    return
            except tk.TclError:
                self._report_popup = None
                self._report_view = None
        win = tk.Toplevel(self)
        win.title("Field matching report")
        win.configure(bg=BG)
        win.geometry("980x720")
        win.minsize(640, 420)
        header = ttk.Frame(win, padding=(16, 12, 16, 6))
        header.pack(fill="x")
        ttk.Label(header, text="Field matching report", font=("Segoe UI", 14, "bold")).pack(side="left")
        ttk.Button(
            header,
            text="Edit conversion table",
            command=lambda: (win.destroy(), self.open_settings(focus_tab="aliases")),
        ).pack(side="right")
        wrap = ttk.Frame(win, padding=(12, 0, 12, 8))
        wrap.pack(fill="both", expand=True)
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        view = tk.Text(
            wrap,
            wrap="word",
            font=("Segoe UI", 10),
            bg=WHITE,
            fg="#1B1B1B",
            relief="flat",
            padx=16,
            pady=12,
            cursor="arrow",
        )
        yscroll = ttk.Scrollbar(wrap, orient="vertical", command=view.yview)
        xscroll = ttk.Scrollbar(wrap, orient="horizontal", command=view.xview)
        view.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        view.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        _configure_markdown_tags(view)
        _fill_markdown_view(view, markdown)
        footer = ttk.Frame(win, padding=(16, 0, 16, 12))
        footer.pack(fill="x")
        ttk.Button(footer, text="Close", command=win.destroy).pack(side="right")

        def close() -> None:
            self._report_popup = None
            self._report_view = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", close)
        self._report_popup = win
        self._report_view = view
        win.lift()
        win.focus_force()

    def _finish_more(self, payload: dict | None) -> None:
        self._busy = False
        self.search_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self._update_list_action_buttons()
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
        errors = int((payload or {}).get("errors") or 0)
        total = int((payload or {}).get("total") or 0)
        publisher = (payload or {}).get("publisher") or "this publisher"
        failed = False
        err_note = ""
        if selected:
            err_note = (selected.extra.get("lookup_note") or "").strip()
        if payload is None:
            failed = True
            summary = "Publisher lookup failed."
        elif payload.get("cancelled"):
            summary = f"Stopped. Filled {updated} of {total} book(s) from {publisher}."
        elif errors and updated:
            summary = (
                f"Filled missing details for {updated} of {total} book(s) from {publisher}. "
                f"{errors} failed because the publisher site returned an error."
            )
        elif errors:
            failed = True
            if err_note:
                summary = f"Publisher lookup failed for {publisher}. {err_note}"
            else:
                summary = (
                    f"Publisher lookup failed for {publisher}. "
                    "The site returned an error, so this is not a missing-book result."
                )
        elif updated:
            summary = f"Filled missing details for {updated} of {total} book(s) from {publisher}. New fields are highlighted in green."
        else:
            summary = (
                f"No matching book page was found on the publisher site for {publisher}. "
                "No new fields were added."
            )
        if payload is None or failed:
            self._end_work("failed")
        elif payload.get("cancelled"):
            self._end_work("stopped")
        else:
            self._end_work("done")
        self._set_status(summary)
        outcome = "failed" if payload is None or failed else "stopped" if (payload or {}).get("cancelled") else "done"
        self._activity.finish(outcome, summary)
        self._complete_lookup_popup(summary, failed=failed)
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

    def _clear_selected_book(self) -> None:
        self._selected_book = None
        self._expanded_detail_keys.clear()
        self._clear_detail_panel()
        self._set_description("")
        self.desc_new_label.pack_forget()
        self.more_btn.configure(state="disabled")
        self._update_workflow_buttons(None)
        try:
            self.table.tree.selection_remove(*self.table.tree.selection())
        except tk.TclError:
            pass

    def _detail_first_line(self, text: str) -> str:
        for line in str(text or "").splitlines():
            stripped = line.strip()
            if stripped:
                return stripped
        return str(text or "").strip()

    def _detail_is_long(self, text: str) -> bool:
        raw = str(text or "").strip()
        if not raw or raw == "—":
            return False
        if "\n" in str(text).strip():
            return True
        return len(raw) > 110

    def _detail_preview(self, text: str) -> str:
        first = self._detail_first_line(text)
        if len(first) > 110:
            return first[:109].rstrip() + "…"
        return first

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
        is_error: bool = False,
        field_key: str = "",
        tone: str = "",
        rtl: bool = False,
    ) -> None:
        del source
        row = self._detail_row
        key = field_key or label
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
        elif tone == "final":
            display = value
            bg, fg = FINAL_BG, FINAL_FG
            font = ("Segoe UI", 10)
        elif tone == "approved":
            display = value
            bg, fg = NEW_BG, NEW_FG
            font = ("Segoe UI", 10)
        elif tone == "error" or is_error:
            display = value
            bg, fg = ERROR_BG, ERROR_FG
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
        expandable = (not empty) and self._detail_is_long(str(value))
        expanded = key in self._expanded_detail_keys
        shown = str(display)
        if expandable and not expanded:
            shown = self._detail_preview(str(value))
        if rtl and not empty:
            from bidi_text import rtl_left_aligned

            shown = rtl_left_aligned(shown)
        val = tk.Label(
            self.detail_inner,
            text=shown,
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
        if expandable:
            full = str(value)

            def toggle(_key=key, _val=val, _full=full, _rtl=rtl) -> None:
                from bidi_text import rtl_left_aligned

                if _key in self._expanded_detail_keys:
                    self._expanded_detail_keys.discard(_key)
                    text = self._detail_preview(_full)
                    _val.configure(text=rtl_left_aligned(text) if _rtl else text)
                    more_btn.configure(text="More")
                else:
                    self._expanded_detail_keys.add(_key)
                    _val.configure(text=rtl_left_aligned(_full) if _rtl else _full)
                    more_btn.configure(text="Less")

            more_btn = tk.Button(
                self.detail_inner,
                text="Less" if expanded else "More",
                command=toggle,
                bg=WHITE,
                fg="#0B57D0",
                activebackground=WHITE,
                activeforeground="#0B57D0",
                relief="flat",
                bd=0,
                highlightthickness=0,
                font=("Segoe UI", 8),
                cursor="hand2",
                padx=0,
                pady=0,
            )
            more_btn.grid(row=row, column=1, sticky="n", pady=6)
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
        self._expanded_detail_keys.clear()
        self._add_detail_section("Scan")
        self._add_detail_row("Scanner ID", book.scanner_id or "—", field_key="scanner_id")
        workflow = "Final" if book.final else "Approved" if book.approved else "Not approved"
        tone = book.display_tone()
        self._add_detail_row("Workflow", workflow, field_key="workflow", tone=tone)
        self._add_detail_row("Status", book.status_label() or "—", field_key="scan_status", tone=tone)
        self._add_detail_row("Created", format_entry_stamp(book.created_at) or "—", field_key="created_at")
        updated = format_entry_stamp(book.modified_at) or "—"
        if book.database_needs_update():
            updated = f"{updated} · new details since last database pass"
        self._add_detail_row("Updated", updated, field_key="modified_at")
        passed = format_entry_stamp(book.database_passed_at)
        if book.database_needs_update():
            passed = f"{passed} · pass again" if passed else "Pass again"
        elif book.excel_passed and not passed:
            passed = "On file"
        elif not passed:
            passed = "Not yet"
        self._add_detail_row("Passed to database", passed, field_key="database_passed_at")
        if book.is_important():
            self._add_detail_row(
                "Attention",
                book.important_reason() or "Needs a database pass",
                field_key="important",
                is_new=True,
            )
        self._add_detail_row("Message", book.scan_message or "—", field_key="scan_message", tone=tone)
        if note:
            self._add_detail_row(
                "Lookup",
                note,
                is_new=not book.extra.get("lookup_error"),
                is_error=bool(book.extra.get("lookup_error")),
                field_key="lookup_note",
            )
        self._add_detail_section("Catalog fields")

        def add_field(key: str, label: str, value: str) -> None:
            is_new = key in new_fields or book.is_external_source(_source_field_key(key))
            link = value if key in {"cover_image_url", "back_image_url"} and str(value or "").startswith("http") else ""
            hebrew_title = key in {"title", "title_he"}
            self._add_detail_row(
                label,
                value,
                is_new=is_new,
                link=link,
                field_key=key,
                rtl=hebrew_title,
            )

        title_he = fields.get("title_he") or book.title or ""
        title_en = fields.get("title_en") or book.title_en or ""
        title_phonetic = fields.get("title_phonetic") or book.title_phonetic or ""
        add_field("title", "Title (Hebrew)", title_he)
        add_field("title_en", "Title in English", title_en)
        add_field("title_phonetic", "Title (phonetics)", title_phonetic)
        shown.update({"title", "title_he", "title_en", "title_phonetic"})

        columns = self.excel_columns or []
        if columns:
            for col in columns:
                field = col.get("field") or ""
                header = str(col.get("header") or "")
                if field in {"description_he", "scanner_id", "title", "title_he", "title_en", "title_phonetic"}:
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
        self._refresh_selection_label()
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
        defaults = {"important", "checked", "approved", "final"}
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
                self._clear_selected_book()
            elif selected:
                self.table.select_book(selected)
                self.show_book(selected)
            self._skip_cache_restore = True
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

    def _prepare_books(self, books: list[Book]) -> int:
        shared = entry_now()
        phonetic_filled = fill_missing_phonetics(books)
        for book in books:
            book.fill_missing_dates(shared)
            if book.author:
                book.author = format_person_name(book.author) or book.author
            if book.translator:
                book.translator = format_person_name(book.translator) or book.translator
            if book.illustrator:
                book.illustrator = format_person_name(book.illustrator) or book.illustrator
            if book.title:
                book.refresh_scan_status()
            elif not book.scan_status:
                book.scan_status = "failed"
                if not book.scan_message:
                    book.scan_message = "Scan did not complete."
        attach_books(books)
        self._sync_books_from_list_excel(books)
        return phonetic_filled

    def _phonetic_fill_message(self, filled: int) -> str:
        import llm_client
        from llm_client import phonetic_status_note

        report = llm_client.last_phonetic_report
        note = phonetic_status_note()
        parts: list[str] = []
        if filled:
            parts.append(
                f"Filled phonetic titles for {filled:,} Hebrew book(s) without marking them updated."
            )
        elif report.succeeded:
            parts.append(
                f"Updated {report.succeeded:,} phonetic titles with the LLM without marking books updated."
            )
        if note:
            if parts:
                parts[-1] = parts[-1].rstrip(".") + f" ({note})."
            else:
                parts.append(note[0].upper() + note[1:] if note else "")
        warning = (report.warning or "").strip()
        if report.skipped_limit and not warning:
            warning = report.error
        if warning:
            parts.append(warning)
            self.after(10, lambda text=warning: messagebox.showwarning("LLM usage", text))
        elif report.error and report.attempted and not report.succeeded:
            parts.append(report.error)
        return " ".join(part for part in parts if part)

    def _refill_phonetics_with_llm(self) -> str:
        if not self.books:
            return ""
        filled = fill_missing_phonetics(self.books)
        self._persist_working()
        self.table.set_books(self.books, keep_checks=True)
        if self._selected_book:
            self.table.select_book(self._selected_book)
        return self._phonetic_fill_message(filled)

    def _book_excel_keys(self, book: Book) -> set[str]:
        keys: set[str] = set()
        if (book.scanner_id or "").strip():
            keys.add(f"scanner:{book.scanner_id.strip()}")
        if (book.isbn or "").strip():
            keys.add(f"isbn:{book.isbn.strip()}")
        title = (book.title or "").strip()
        if title:
            keys.add(f"title:{title.casefold()}")
        return keys

    def _sync_books_from_list_excel(self, books: list[Book]) -> None:
        path = Path(self.excel_path.get().strip())
        present: set[str] = set()
        if path.exists():
            try:
                catalog = CatalogWorkbook(path)
                present = catalog.existing_keys()
            except Exception:
                present = set()
        for book in books:
            on_excel = bool(present and (self._book_excel_keys(book) & present))
            if on_excel:
                book.excel_passed = True
                book.final = True
                book.approved = True
            else:
                book.excel_passed = False
                book.final = False

    def _update_workflow_buttons(self, book: Book | None) -> None:
        if book is None or self._busy or self._list_locked:
            self.approve_btn.configure(state="disabled")
            self.final_btn.configure(state="disabled")
            self.unfinal_btn.configure(state="disabled")
            return
        can_approve = bool(book) and (not book.final) and (not book.approved) and bool((book.title or "").strip())
        can_final = bool(book) and book.approved and (not book.final)
        can_unfinal = bool(book) and book.final
        self.approve_btn.configure(state="normal" if can_approve else "disabled")
        self.final_btn.configure(state="normal" if can_final else "disabled")
        self.unfinal_btn.configure(state="normal" if can_unfinal else "disabled")

    def approve_selected(self) -> None:
        if self._guard_locked("approve books"):
            return
        if self._selected_book:
            self._approve_books([self._selected_book])

    def mark_selected_final(self) -> None:
        if self._guard_locked("change this list's Excel"):
            return
        if self._selected_book:
            self._mark_books_final([self._selected_book])

    def remove_selected_final(self) -> None:
        if self._guard_locked("change this list's Excel"):
            return
        book = self._selected_book
        if not book or not book.final:
            return
        if not messagebox.askyesno(
            "Remove final",
            f"Remove Final from “{book.display_title()}”?\n\n"
            "This takes the book off this list’s Excel and brings it back to Approved.",
        ):
            return
        path = Path(self.excel_path.get().strip())
        if path.exists() and (book.scanner_id or "").strip():
            try:
                catalog = CatalogWorkbook(path)
                catalog.remove_scanner_ids({book.scanner_id})
                saved = catalog.save()
            except Exception as exc:
                messagebox.showerror("Excel", f"Could not update the Excel file:\n{exc}")
                return
            if saved != path:
                messagebox.showinfo(
                    "Excel",
                    f"The original file is open or locked, so the change was saved as:\n{saved}",
                )
        book.final = False
        book.excel_passed = False
        book.database_passed_at = ""
        book.approved = True
        persist_book_state(book)
        self.table.refresh_book(book)
        self.show_book(book)
        self._persist_working()
        self.refresh_excel_info()
        self._set_status("Removed Final. The book is Approved again and is not on this list’s Excel.")

    def _approve_books(self, books: list[Book]) -> None:
        approved = 0
        skipped_final = 0
        skipped_failed = 0
        already = 0
        for book in books:
            attach_book(book)
            if book.final:
                skipped_final += 1
                continue
            if not (book.title or "").strip():
                skipped_failed += 1
                continue
            if book.approved:
                already += 1
                continue
            book.approved = True
            persist_book_state(book)
            self.table.refresh_book(book)
            approved += 1
        if self._selected_book:
            self.show_book(self._selected_book)
            self._update_workflow_buttons(self._selected_book)
        self._persist_working()
        parts = []
        if approved:
            parts.append(f"Approved {approved} book(s)")
        if already:
            parts.append(f"{already} already approved")
        if skipped_failed:
            parts.append(f"{skipped_failed} need a title first")
        if skipped_final:
            parts.append(f"{skipped_final} already final")
        self._set_status("; ".join(parts) or "Nothing to approve.")

    def _mark_books_final(self, books: list[Book]) -> None:
        to_write: list[Book] = []
        already_in_excel: list[Book] = []
        skipped_unapproved = 0
        skipped_final = 0
        for book in books:
            attach_book(book)
            if book.final:
                skipped_final += 1
                continue
            if not book.approved:
                skipped_unapproved += 1
                continue
            if book.excel_passed:
                already_in_excel.append(book)
            else:
                to_write.append(book)
        written = 0
        skipped_excel = 0
        saved = None
        note = ""
        if to_write:
            written, skipped_excel, saved, note = self._pass_books_to_excel(to_write)
        marked = 0
        for book in [*to_write, *already_in_excel]:
            if not book.excel_passed:
                continue
            book.final = True
            book.approved = True
            persist_book_state(book)
            self.table.refresh_book(book)
            marked += 1
        if self._selected_book:
            self.show_book(self._selected_book)
            self._update_workflow_buttons(self._selected_book)
        self._persist_working()
        if not marked:
            if skipped_unapproved:
                messagebox.showinfo(
                    "Mark final",
                    "Approve the book in the details pane first. Mark final writes it to Excel and marks it as on the spreadsheet.",
                )
            elif skipped_final:
                self._set_status("This book is already final and in Excel.")
            else:
                messagebox.showinfo("Mark final", "The book could not be written to Excel.")
            return
        parts = [f"Marked {marked} book(s) final"]
        if written:
            parts.append(f"wrote {written} to Excel")
        if skipped_excel:
            parts.append(f"{skipped_excel} already in Excel")
        if skipped_unapproved:
            parts.append(f"{skipped_unapproved} not approved yet")
        summary = "; ".join(parts)
        if saved and note:
            summary += note
        self._set_status(summary)

    def _pass_books_to_excel(self, books: list[Book]) -> tuple[int, int, Path | None, str]:
        if self._list_locked:
            messagebox.showinfo("Locked list", "This list is locked, so its Excel cannot be changed.")
            return 0, 0, None, ""
        path = self._ensure_list_excel(create=True)
        if path is None:
            return 0, 0, None, ""
        try:
            catalog = CatalogWorkbook(path)
            payload = [book.to_excel_fields() for book in books]
            written, skipped, written_ids, skipped_ids = catalog.append_books(payload)
            saved = catalog.save()
        except Exception as exc:
            messagebox.showerror("Could not write Excel", str(exc))
            return 0, 0, None, ""
        written_set = set(written_ids)
        skipped_set = set(skipped_ids)
        for book in books:
            if book.scanner_id in written_set:
                book.approved = True
                book.excel_passed = True
                book.stamp_database_passed()
                persist_book_state(book)
            elif book.scanner_id in skipped_set:
                book.approved = True
                book.excel_passed = True
                persist_book_state(book)
        self.refresh_excel_info()
        note = ""
        if saved != path:
            note = f"\n\nThe original file is open or locked, so results were saved as:\n{saved}"
        return written, skipped, saved, note

    def _set_status(self, text: str) -> None:
        self.status.set(text)
        self._sync_work_progress(text)

    def _page_limit_setting(self) -> int:
        if bool(self.no_page_limit.get()):
            return 0
        try:
            return max(1, int(self.max_pages.get() or 40))
        except (TypeError, ValueError, tk.TclError):
            return 40

    def _on_page_limit_toggle(self) -> None:
        self._sync_page_limit_state()
        self._on_list_fields_changed()

    def _sync_page_limit_state(self) -> None:
        locked = bool(self._list_locked)
        try:
            self.no_limit_check.configure(state="disabled" if locked else "normal")
        except tk.TclError:
            pass
        if locked or bool(self.no_page_limit.get()):
            self.pages_spin.configure(state="disabled")
        else:
            self.pages_spin.configure(state="normal")

    def _set_pages_total(self, total: int) -> None:
        try:
            self._pages_total = max(0, int(total or 0))
        except (TypeError, ValueError):
            self._pages_total = 0
        if self._pages_total:
            self.pages_label.configure(text=f"Max pages ({self._pages_total})")
        else:
            self.pages_label.configure(text="Max pages")

    def _highlight_site_url(self, url: str) -> None:
        box = self.url_text
        try:
            box.tag_remove("searching", "1.0", "end")
        except tk.TclError:
            return
        if not url:
            return
        target = listing_url_key(url)
        host = site_host(url)
        lines = box.get("1.0", "end-1c").splitlines()
        for index, line in enumerate(lines, start=1):
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            if listing_url_key(raw) == target or site_host(raw) == host:
                box.tag_add("searching", f"{index}.0", f"{index}.end")
                box.see(f"{index}.0")
                return

    def _refresh_scan_live(
        self,
        site: str = "",
        found: int | None = None,
        checking: int | None = None,
        total: int | None = None,
        phase: str = "",
        title: str = "",
        author: str = "",
        publisher: str = "",
    ) -> None:
        if site:
            self._scan_site = site
        if found is not None:
            self._scan_found = found
        if checking is not None:
            self._scan_checking = checking
        if total is not None and total > 0:
            self._scan_total = total
        if phase:
            self._scan_phase = phase
        if title:
            who = title
            if author:
                who += f" · {author}"
            if publisher:
                who += f" · {publisher}"
            self._scan_book = who
        site_name = self._scan_site
        found_count = len(self.books) if self.books else self._scan_found
        checking_count = self._scan_checking
        total_count = self._scan_total
        parts: list[str] = []
        if self._scan_phase:
            parts.append(self._scan_phase)
        if site_name:
            parts.append(site_name)
        parts.append(f"{found_count:,} on the list")
        if self._scan_phase == "Filling extra details" and total_count:
            parts.append(f"checking {checking_count:,} of {total_count:,}")
        elif checking_count:
            parts.append(f"listing {checking_count:,}")
        if self._scan_book:
            parts.append(self._scan_book)
        self.scan_live.set("  ·  ".join(parts))
        if self._busy and total_count:
            self._set_progress_count(checking_count, total_count)
            self.work_hint.set(f"{checking_count:,} / {total_count:,}")

    def _prepare_one_book(self, book: Book) -> None:
        book.refresh_text_fields()
        book.stamp_created()
        if book.author:
            book.author = format_person_name(book.author) or book.author
        if book.translator:
            book.translator = format_person_name(book.translator) or book.translator
        if book.illustrator:
            book.illustrator = format_person_name(book.illustrator) or book.illustrator
        if book.title:
            book.refresh_scan_status()
        try:
            attach_books([book], save=False)
        except Exception:
            pass

    def _queue_detail(self, book: Book) -> None:
        self._pending_detail = book
        if self._detail_job:
            return
        self._detail_job = self.after(200, self._flush_detail)

    def _flush_detail(self) -> None:
        self._detail_job = None
        book = self._pending_detail
        if book is None:
            return
        self._programmatic_select = True
        try:
            self.show_book(book)
        finally:
            self._programmatic_select = False

    def _select_live_book(self, book: Book) -> None:
        if not self._follow_search:
            if book is self._selected_book:
                self.table.refresh_book(book)
                self._queue_detail(book)
            return
        self._programmatic_select = True
        try:
            self.table.select_book(book)
            self._queue_detail(book)
        finally:
            self._programmatic_select = False

    def _ingest_search_book(self, book: Book) -> None:
        def same(item: Book) -> bool:
            if item is book or item.key() == book.key():
                return True
            if book.scanner_id and item.scanner_id == book.scanner_id:
                return True
            left = (item.url or "").strip()
            right = (book.url or "").strip()
            if left and left == right:
                return True
            return books_match(item, book)

        existing = next((item for item in self.books if same(item)), None)
        if existing is None:
            self._prepare_one_book(book)
            self.books.append(book)
            self.table.add_row(book)
            self._select_live_book(book)
            self._schedule_live_save()
            self._refresh_selection_label()
            self._set_running_summary()
            return
        if existing is not book:
            merge_later_into(existing, book, fallback_now=True)
        self.table.refresh_book(existing)
        self._select_live_book(existing)
        self._schedule_live_save()
        self._refresh_selection_label()
        self._set_running_summary()

    def _schedule_live_save(self) -> None:
        if self._live_save_job:
            return
        self._live_save_job = self.after(1600, self._flush_live_save)

    def _flush_live_save(self) -> None:
        self._live_save_job = None
        try:
            from scanner_registry import save_registry

            save_registry()
        except Exception:
            pass
        if not self.books:
            return
        try:
            save_working(self._current_payload())
        except Exception:
            pass

    def _cancel_live_save(self) -> None:
        job = self._live_save_job
        self._live_save_job = None
        if job:
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass
        try:
            from scanner_registry import save_registry

            save_registry()
        except Exception:
            pass

    def _on_search_event(self, kind: str, data: dict) -> None:
        site = str(data.get("site") or "")
        url = str(data.get("url") or "")
        if kind == "pages":
            total = int(data.get("total") or 0)
            current = int(data.get("current") or 0)
            if total:
                self._set_pages_total(total)
            self._refresh_scan_live(site=site, found=len(self.books), phase="Listing")
            if site and total:
                cap = " · no limit" if data.get("unlimited") else ""
                self._set_status(f"{site}: catalog page {current:,} of {total:,}{cap}")
            return
        if kind == "site":
            self._highlight_site_url(url)
            phase = str(data.get("phase") or self._scan_phase or "Listing")
            self._refresh_scan_live(site=site, found=len(self.books), phase=phase)
            if site:
                verb = "Filling extra details from" if phase == "fill" else "Listing"
                self._set_status(f"{verb} {site}…")
            return
        if kind in {"check", "fill"}:
            self._highlight_site_url(url)
            book = data.get("book")
            title = str(data.get("title") or "")
            author = str(data.get("author") or "")
            publisher = str(data.get("publisher") or "")
            if isinstance(book, Book):
                self._ingest_search_book(book)
                title = title or book.display_title()
                author = author or book.author
                publisher = publisher or book.publisher
            phase = "Filling extra details" if kind == "fill" else "Listing"
            self._refresh_scan_live(
                site=site,
                found=len(self.books),
                checking=int(data.get("index") or 0),
                total=int(data.get("total") or 0),
                phase=phase,
                title=title,
                author=author,
                publisher=publisher,
            )
            self._set_running_summary()
            return
        if kind == "book":
            book = data.get("book")
            if isinstance(book, Book):
                self._ingest_search_book(book)
            self._refresh_scan_live(site=site, found=len(self.books))
            self._set_running_summary()

    def _begin_work(self, hint: str = "Working…") -> None:
        self.work_hint.set(hint)
        self._set_progress_busy()

    def _end_work(self, outcome: str = "") -> None:
        try:
            self.progress.stop()
        except tk.TclError:
            pass
        self._progress_determinate = True
        if outcome == "done":
            self.progress.configure(mode="determinate", maximum=100, value=100)
            self.work_hint.set("Done")
        elif outcome == "stopped":
            self.progress.configure(mode="determinate", maximum=100, value=0)
            self.work_hint.set("Stopped")
        elif outcome == "failed":
            self.progress.configure(mode="determinate", maximum=100, value=0)
            self.work_hint.set("Failed")
        else:
            self.progress.configure(mode="determinate", maximum=100, value=0)
            self.work_hint.set("")

    def _set_progress_busy(self) -> None:
        if (not self._progress_determinate) and str(self.progress.cget("mode")) == "indeterminate":
            return
        try:
            self.progress.stop()
        except tk.TclError:
            pass
        self.progress.configure(mode="indeterminate")
        self._progress_determinate = False
        self.progress.start(12)

    def _set_progress_count(self, current: int, total: int) -> None:
        total = max(int(total), 1)
        current = max(0, min(int(current), total))
        if not self._progress_determinate:
            try:
                self.progress.stop()
            except tk.TclError:
                pass
            self.progress.configure(mode="determinate")
            self._progress_determinate = True
        self.progress.configure(maximum=total, value=current)

    def _sync_work_progress(self, text: str) -> None:
        if not self._busy:
            return
        match = re.search(r"(?<!\d)(\d+)\s*(?:/|of)\s*(\d+)(?!\d)", text, re.I)
        if match:
            current, total = int(match.group(1)), int(match.group(2))
            if total > 0:
                self._set_progress_count(current, total)
                self.work_hint.set(f"{current} / {total}")
                return
        self._set_progress_busy()
        self.work_hint.set("Working…")

    def _schedule_update_check(self, delay_ms: int) -> None:
        if self._updating:
            return
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        if self._update_check_after is not None:
            try:
                self.after_cancel(self._update_check_after)
            except tk.TclError:
                pass
        try:
            self._update_check_after = self.after(delay_ms, self._periodic_update_check)
        except tk.TclError:
            self._update_check_after = None

    def _periodic_update_check(self) -> None:
        self._update_check_after = None
        try:
            if self._updating or not self.winfo_exists():
                return
        except tk.TclError:
            return
        self.check_for_updates(silent=True)
        self._schedule_update_check(UPDATE_CHECK_EVERY_MS)

    def check_for_updates(self, silent: bool = False) -> None:
        if self._updating:
            return
        if self._update_check_running:
            if not silent:
                self._set_status("Already checking GitHub for a SISU update…")
            return
        if self._busy:
            if not silent:
                messagebox.showinfo(
                    "Update",
                    "Wait until Search or More finishes, then check for updates again.",
                )
            return
        self._update_check_running = True
        if not silent:
            self._set_status("Checking GitHub for a newer SISU…")
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
        self._update_check_running = False
        if info is None:
            if silent:
                return
            messagebox.showinfo("Update", "SISU is already up to date.")
            self._set_status("SISU is already up to date.")
            return
        if self._busy:
            self.after(10_000, lambda: self._on_update_result(silent, info))
            return
        from app_update import UpdateInfo

        if not isinstance(info, UpdateInfo):
            return
        if silent and self._update_declined_remote == info.remote:
            self._set_status("A newer SISU version is available. Click Check for updates when you are ready.")
            return
        if info.dirty:
            listed = "\n".join(f"• {path}" for path in (info.code_files or [])[:8])
            extra = "" if len(info.code_files) <= 8 else f"\n• … and {len(info.code_files) - 8} more"
            message = (
                "A newer SISU version is available, but this copy has local program-file changes, "
                "so it cannot update itself.\n\n"
                "Update this folder by hand (or stash those files, pull, then restore them).\n\n"
                f"{listed}{extra}"
            )
            if silent:
                self._set_status("A newer SISU is available, but local program files have changes. Update by hand.")
                return
            messagebox.showwarning("Cannot update automatically", message)
            return
        if not messagebox.askokcancel(
            "Update SISU",
            "A newer version of SISU is available.\n\n"
            "If you continue, SISU will:\n"
            "• stash local JSON cache files if they would block the download\n"
            "• download the update from GitHub\n"
            "• put the JSON cache back\n"
            "• install any new libraries\n"
            "• close this window and start the new version\n\n"
            "Program files you edited yourself are not updated automatically — "
            "those still need a manual update.\n\n"
            "Update and restart now?",
        ):
            self._update_declined_remote = info.remote
            self._set_status("Update postponed. SISU will check GitHub again later, or click Check for updates.")
            return
        self._update_declined_remote = ""
        self._apply_self_update()

    def _on_update_error(self, silent: bool, text: str) -> None:
        self._update_check_running = False
        self._updating = False
        if self._busy:
            return
        self.search_btn.configure(state="normal")
        if silent:
            return
        messagebox.showerror("Update failed", text)

    def _apply_self_update(self) -> None:
        self._updating = True
        self.search_btn.configure(state="disabled")
        self.more_btn.configure(state="disabled")
        self.approve_btn.configure(state="disabled")
        self.final_btn.configure(state="disabled")
        self.unfinal_btn.configure(state="disabled")
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
            self.after(400, self._hot_replace)
            return
        self.after(800, self._watch_for_reload)

    def _hot_replace(self) -> None:
        self._restart_app("Code changed — restarting window, cache kept…")

    def _restart_app(self, reason: str = "") -> None:
        self._updating = True
        if self._update_check_after is not None:
            try:
                self.after_cancel(self._update_check_after)
            except tk.TclError:
                pass
            self._update_check_after = None
        if reason:
            self._set_status(reason)
        self.update_idletasks()
        from app_update import restart_process

        restart_process()
        self.after(150, self.destroy)


def _configure_markdown_tags(view: tk.Text) -> None:
    view.tag_configure("h1", font=("Segoe UI", 18, "bold"), foreground=NAVY, spacing1=8, spacing3=10)
    view.tag_configure("h2", font=("Segoe UI", 14, "bold"), foreground=NAVY, spacing1=12, spacing3=8)
    view.tag_configure("h3", font=("Segoe UI", 12, "bold"), foreground=NAVY, spacing1=10, spacing3=6)
    view.tag_configure("p", font=("Segoe UI", 10), spacing3=8)
    view.tag_configure("li", font=("Segoe UI", 10), lmargin1=22, lmargin2=40, spacing3=3)
    view.tag_configure("code", font=("Consolas", 10), background="#F4F1EA")
    view.tag_configure("table", font=("Consolas", 9), wrap="none", spacing3=8)
    view.tag_configure("th", font=("Consolas", 9, "bold"), wrap="none")
    view.tag_configure("muted", foreground="#5A6570")
    view.tag_configure("ok", foreground=NEW_FG)
    view.tag_configure("bad", foreground=ERROR_FG)
    view.tag_configure("warn", foreground="#8A5A00")


def _md_table_cells(line: str) -> list[str]:
    parts = [part.strip() for part in line.strip().strip("|").split("|")]
    return parts


def _md_is_separator(line: str) -> bool:
    cells = _md_table_cells(line)
    if not cells:
        return False
    return all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells)


def _format_md_table(rows: list[list[str]]) -> tuple[str, int]:
    if not rows:
        return "", 0
    width = max(len(row) for row in rows)
    normalized: list[list[str]] = []
    for row in rows:
        cells = list(row) + [""] * (width - len(row))
        wrapped: list[str] = []
        for cell in cells:
            text = re.sub(r"\s+", " ", cell).strip()
            if len(text) > 72:
                text = text[:71].rstrip() + "…"
            wrapped.append(text)
        normalized.append(wrapped)
    col_w = [1] * width
    for row in normalized:
        for index, cell in enumerate(row):
            col_w[index] = max(col_w[index], len(cell))
    lines: list[str] = []
    for row_index, row in enumerate(normalized):
        line = "  ".join(cell.ljust(col_w[index]) for index, cell in enumerate(row))
        lines.append(line)
        if row_index == 0:
            lines.append("  ".join("-" * col_w[index] for index in range(width)))
    return "\n".join(lines) + "\n", 0 if not normalized else 0


def _insert_inline(view: tk.Text, text: str, base_tag: str) -> None:
    pattern = re.compile(r"(`[^`]+`|\*\*[^*]+\*\*)")
    start = 0
    for match in pattern.finditer(text):
        if match.start() > start:
            view.insert("end", text[start : match.start()], base_tag)
        token = match.group(0)
        if token.startswith("`"):
            view.insert("end", token[1:-1], (base_tag, "code"))
        else:
            view.insert("end", token[2:-2], (base_tag, "h3"))
        start = match.end()
    if start < len(text):
        view.insert("end", text[start:], base_tag)


def _fill_markdown_view(view: tk.Text, source: str) -> None:
    view.configure(state="normal")
    view.delete("1.0", "end")
    lines = (source or "").replace("\r\n", "\n").split("\n")
    index = 0
    blank = 0
    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        if not stripped or stripped == "#":
            blank += 1
            index += 1
            if blank == 1:
                view.insert("end", "\n")
            continue
        blank = 0
        if stripped.startswith("|"):
            table_lines: list[str] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                if not _md_is_separator(lines[index]):
                    table_lines.append(lines[index])
                index += 1
            rows = [_md_table_cells(line) for line in table_lines]
            formatted, _unused = _format_md_table(rows)
            del _unused
            start = view.index("end-1c")
            view.insert("end", formatted + "\n", "table")
            if rows:
                header_line = formatted.split("\n", 1)[0]
                view.tag_add("th", start, f"{start}+{len(header_line)}c")
            continue
        if stripped.startswith("### "):
            _insert_inline(view, stripped[4:] + "\n", "h3")
            index += 1
            continue
        if stripped.startswith("## "):
            _insert_inline(view, stripped[3:] + "\n", "h2")
            index += 1
            continue
        if stripped.startswith("# "):
            _insert_inline(view, stripped[2:] + "\n", "h1")
            index += 1
            continue
        if re.match(r"^[-*]\s+", stripped):
            item = re.sub(r"^[-*]\s+", "• ", stripped)
            _insert_inline(view, item + "\n", "li")
            index += 1
            continue
        numbered = re.match(r"^(\d+)\.\s+", stripped)
        if numbered:
            _insert_inline(view, stripped + "\n", "li")
            index += 1
            continue
        _insert_inline(view, stripped + "\n", "p")
        index += 1
    view.configure(state="disabled")


def _bind_entry_clipboard(entry: tk.Entry) -> None:
    def paste(event: tk.Event) -> str:
        widget = event.widget
        try:
            clip = str(widget.clipboard_get())
        except tk.TclError:
            return "break"
        try:
            widget.delete("sel.first", "sel.last")
        except tk.TclError:
            pass
        widget.insert("insert", clip)
        return "break"

    for sequence in ("<<Paste>>", "<Control-v>", "<Control-V>", "<Shift-Insert>"):
        entry.bind(sequence, paste)


def _bind_combobox_clipboard(combo: ttk.Combobox) -> None:
    def paste(_event: tk.Event) -> str:
        try:
            clip = str(combo.clipboard_get())
        except tk.TclError:
            return "break"
        try:
            combo.delete("sel.first", "sel.last")
        except tk.TclError:
            pass
        combo.insert("insert", clip)
        return "break"

    for sequence in ("<<Paste>>", "<Control-v>", "<Control-V>", "<Shift-Insert>"):
        combo.bind(sequence, paste)


def _settings_scroll_table(
    parent: tk.Widget,
    columns: tuple[int, ...] = (1,),
    height: int | None = None,
) -> ttk.Frame:
    canvas = tk.Canvas(parent, bg=BG, highlightthickness=0)
    if height:
        canvas.configure(height=height)
    scroll = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
    inner = ttk.Frame(canvas)
    for column in columns:
        inner.columnconfigure(column, weight=1)
    window_id = canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.configure(yscrollcommand=scroll.set)
    canvas.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")
    inner._settings_canvas = canvas
    inner._settings_window_id = window_id
    inner._suspend_sync = False

    def sync(_event=None) -> None:
        if getattr(inner, "_suspend_sync", False):
            return
        width = int(canvas.winfo_width() or 0)
        if width > 20:
            canvas.itemconfigure(window_id, width=width)
        bbox = canvas.bbox("all")
        if bbox:
            canvas.configure(scrollregion=bbox)

    inner.bind("<Configure>", sync)
    canvas.bind("<Configure>", sync)

    def wheel(event: tk.Event) -> str:
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        return "break"

    canvas.bind("<MouseWheel>", wheel)
    inner.bind("<MouseWheel>", wheel)
    return inner


def _suspend_settings_table(inner: ttk.Frame, paused: bool) -> None:
    inner._suspend_sync = paused
    if paused:
        return
    canvas = getattr(inner, "_settings_canvas", None)
    window_id = getattr(inner, "_settings_window_id", None)
    if canvas is None or window_id is None:
        return
    width = int(canvas.winfo_width() or 0)
    if width > 20:
        canvas.itemconfigure(window_id, width=width)
    bbox = canvas.bbox("all")
    if bbox:
        canvas.configure(scrollregion=bbox)


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
