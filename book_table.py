"""Fast native table with proportional columns and click-to-sort."""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from typing import Callable

from book_crawler import Book, format_price

ROW_PAD = 6
MARK_WIDTH = 36
WEIGHTS = {
    "title": 2.0,
    "author": 0.9,
    "year": 0.45,
    "status": 0.8,
    "publisher": 0.9,
    "code": 0.8,
    "price": 0.5,
}
HEADINGS = {
    "mark": "☑",
    "title": "Title",
    "author": "Author",
    "year": "Year",
    "status": "Status",
    "publisher": "Publisher",
    "code": "ISBN / code",
    "price": "Price ₪",
}
ROW_STATUSES = (
    ("checked", "Checked"),
    ("approved", "Approved"),
    ("failed", "Errors"),
    ("successful", "Successful"),
    ("fully scanned", "Fully scanned"),
    ("final", "Final"),
    ("excel", "In Excel"),
)


class BookTable(ttk.Frame):
    def __init__(
        self,
        parent: tk.Widget,
        on_select: Callable[[Book], None] | None = None,
        on_check: Callable[[], None] | None = None,
        on_publisher: Callable[[Book], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.on_select = on_select
        self.on_check = on_check
        self.on_publisher = on_publisher
        self.books: list[Book] = []
        self.order: list[int] = []
        self.checked: set[str] = set()
        self.sort_column = "title"
        self.sort_reverse = False
        self.filter_keys: set[str] = set()
        self._by_iid: dict[str, Book] = {}

        style = ttk.Style(self)
        style.configure("Books.Treeview", font=("Segoe UI", 10), padding=0)
        style.configure("Books.Treeview.Heading", font=("Segoe UI", 9, "bold"))
        self._row_font = tkfont.Font(self, family="Segoe UI", size=10)
        self._apply_rowheight()

        columns = ("mark", "title", "author", "year", "status", "publisher", "code", "price")
        self.tree = ttk.Treeview(
            self,
            columns=columns,
            show="headings",
            selectmode="browse",
            style="Books.Treeview",
        )
        for key, label in HEADINGS.items():
            self.tree.heading(key, text=self._header_text(key, label), command=lambda k=key: self.toggle_sort(k))
            if key == "price":
                anchor = "e"
            elif key in {"mark", "author", "year", "status", "publisher", "code"}:
                anchor = "center"
            else:
                anchor = "w"
            self.tree.column(key, anchor=anchor, stretch=key != "mark", width=80)
        self.tree.column("mark", width=MARK_WIDTH, stretch=False, anchor="center")
        self.tree.column("price", anchor="e")
        self.tree.tag_configure("failed", background="#FDECEC", foreground="#B42318")
        self.tree.tag_configure("approved", background="#E4F7EA", foreground="#146C43")
        self.tree.tag_configure("final", background="#E8F0FE", foreground="#0B57D0")
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Motion>", self._on_motion)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.bind("<Configure>", self._on_resize)
        self.after_idle(self._apply_rowheight)

    def _apply_rowheight(self) -> None:
        """Size rows to the font on this machine so high-DPI Windows is not extra-tall."""
        line = int(self._row_font.metrics("linespace") or 16)
        height = max(22, line + ROW_PAD)
        ttk.Style(self).configure("Books.Treeview", rowheight=height)

    def _header_text(self, key: str, label: str) -> str:
        if key not in {"mark", "title", "author", "publisher", "status"}:
            return label
        if self.sort_column != key:
            return f"{label}  ↕"
        return f"{label}  {'↓' if self.sort_reverse else '↑'}"

    def _on_resize(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        self._apply_column_widths(event.width)

    def _apply_column_widths(self, total_width: int) -> None:
        usable = max(420, total_width - MARK_WIDTH - 22)
        weight_sum = sum(WEIGHTS.values())
        widths = {key: int(usable * weight / weight_sum) for key, weight in WEIGHTS.items()}
        other_max = max(width for key, width in widths.items() if key != "title")
        cap = other_max * 2
        if widths["title"] > cap:
            widths["title"] = cap
        self.tree.column("mark", width=MARK_WIDTH, stretch=False)
        for key, width in widths.items():
            self.tree.column(key, width=max(48, width), stretch=True)

    def set_books(
        self,
        books: list[Book],
        keep_checks: bool = True,
        key_map: dict[str, str] | None = None,
    ) -> None:
        previous = set(self.checked) if keep_checks else set()
        if key_map:
            previous = {key_map.get(key, key) for key in previous}
        self.books = books
        self.checked = {book.key() for book in books if book.key() in previous}
        self.order = list(range(len(books)))
        self._sort_order()
        self._reload()
        self._after_check_change()

    def add_row(self, book: Book) -> None:
        try:
            index = next(i for i, item in enumerate(self.books) if item is book)
        except StopIteration:
            self.books.append(book)
            index = len(self.books) - 1
        if index not in self.order:
            self.order.append(index)
        if not self._matches_filter(book):
            return
        iid = f"{index}:{book.key()}"
        if iid in self._by_iid:
            self.refresh_book(book)
            return
        self._by_iid[iid] = book
        self.tree.insert("", "end", iid=iid, values=self._row_values(book), tags=self._row_tags(book))

    def set_filters(self, keys: set[str] | None) -> None:
        self.filter_keys = {key for key in (keys or set()) if key}
        self._reload()

    def book_has_status(self, book: Book, key: str) -> bool:
        if key == "checked":
            return book.key() in self.checked
        if key == "approved":
            return bool(book.approved)
        if key == "final":
            return bool(book.final)
        if key == "excel":
            return bool(book.excel_passed)
        if key == "failed":
            return (book.scan_status or "") == "failed"
        if key == "successful":
            return (book.scan_status or "") == "successful"
        if key == "fully scanned":
            return (book.scan_status or "") == "fully scanned"
        return False

    def _matches_filter(self, book: Book) -> bool:
        if not self.filter_keys:
            return True
        return any(self.book_has_status(book, key) for key in self.filter_keys)

    def select_book(self, book: Book) -> None:
        for iid, item in self._by_iid.items():
            if item is book or item.key() == book.key():
                self.tree.selection_set(iid)
                self.tree.see(iid)
                return

    def toggle_sort(self, column: str) -> None:
        if column not in {"mark", "title", "author", "publisher", "status"}:
            return
        if self.sort_column == column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = column
            self.sort_reverse = False
        for key, label in HEADINGS.items():
            self.tree.heading(key, text=self._header_text(key, label), command=lambda k=key: self.toggle_sort(k))
        self._sort_order()
        self._reload()

    def _sort_order(self) -> None:
        def value(index: int):
            book = self.books[index]
            title = book.display_title().casefold()
            if self.sort_column == "mark":
                return (0 if book.key() in self.checked else 1, title)
            if self.sort_column == "author":
                return book.author.casefold()
            if self.sort_column == "publisher":
                return book.publisher.casefold()
            if self.sort_column == "status":
                return (book.status_label().casefold(), title)
            return title

        self.order.sort(key=value, reverse=self.sort_reverse)

    def selected_books(self) -> list[Book]:
        return [book for book in self.books if book.key() in self.checked]

    def _after_check_change(self) -> None:
        if self.on_check:
            self.on_check()
        if "checked" in self.filter_keys:
            self._reload()

    def select_all(self) -> None:
        self.checked = {book.key() for book in self.books if self._matches_filter(book)}
        self._refresh_marks()
        self._after_check_change()

    def clear_selection(self) -> None:
        self.checked.clear()
        self._refresh_marks()
        self._after_check_change()

    def _row_values(self, book: Book) -> tuple[str, ...]:
        return (
            "☑" if book.key() in self.checked else "☐",
            book.display_title(),
            book.author,
            book.year,
            book.status_label(),
            book.publisher,
            book.identity_code() or book.scanner_id,
            format_price(book.price_ils),
        )

    def _row_tags(self, book: Book) -> tuple[str, ...]:
        tone = book.display_tone()
        if tone:
            return ("failed" if tone == "error" else tone,)
        return ()

    def _reload(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._by_iid.clear()
        for book_index in self.order:
            book = self.books[book_index]
            if not self._matches_filter(book):
                continue
            key = book.key()
            iid = f"{book_index}:{key}"
            self._by_iid[iid] = book
            self.tree.insert("", "end", iid=iid, values=self._row_values(book), tags=self._row_tags(book))
        self.after_idle(lambda: self._apply_column_widths(self.winfo_width() or 800))

    def refresh_book(self, book: Book) -> None:
        for iid, item in self._by_iid.items():
            if item is book or item.key() == book.key():
                self.tree.item(iid, values=self._row_values(book), tags=self._row_tags(book))
                return
        self._reload()

    def _refresh_marks(self) -> None:
        for iid in self.tree.get_children():
            book = self._by_iid.get(iid)
            if not book:
                continue
            values = list(self.tree.item(iid, "values"))
            values[0] = "☑" if book.key() in self.checked else "☐"
            self.tree.item(iid, values=values)

    def _column_at(self, event: tk.Event) -> str | None:
        col = self.tree.identify_column(event.x)
        if not col.startswith("#"):
            return None
        index = int(col[1:]) - 1
        keys = self.tree["columns"]
        if 0 <= index < len(keys):
            return str(keys[index])
        return None

    def _on_motion(self, event: tk.Event) -> None:
        if self.tree.identify("region", event.x, event.y) != "cell":
            self.tree.configure(cursor="")
            return
        column = self._column_at(event)
        self.tree.configure(cursor="hand2" if column == "publisher" else "")

    def _on_click(self, event: tk.Event) -> None:
        if self.tree.identify("region", event.x, event.y) != "cell":
            return
        column = self._column_at(event)
        row = self.tree.identify_row(event.y)
        book = self._by_iid.get(row)
        if not book:
            return
        if column == "mark":
            key = book.key()
            if key in self.checked:
                self.checked.discard(key)
            else:
                self.checked.add(key)
            values = list(self.tree.item(row, "values"))
            values[0] = "☑" if key in self.checked else "☐"
            self.tree.item(row, values=values)
            self._after_check_change()
            return
        if column == "publisher" and self.on_publisher:
            self.after_idle(lambda b=book: self.on_publisher(b))

    def _on_select(self, _event=None) -> None:
        selection = self.tree.selection()
        if not selection or not self.on_select:
            return
        book = self._by_iid.get(selection[0])
        if book:
            self.on_select(book)
