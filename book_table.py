"""Fast native table with proportional columns and click-to-sort."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable

from book_crawler import Book, format_price

ROW_HEIGHT = 32
MARK_WIDTH = 36
WEIGHTS = {
    "title": 2.0,
    "author": 1.0,
    "year": 0.55,
    "publisher": 1.0,
    "code": 0.9,
    "price": 0.55,
}


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
        self._by_iid: dict[str, Book] = {}

        style = ttk.Style(self)
        style.configure("Books.Treeview", font=("Segoe UI", 10), rowheight=ROW_HEIGHT)
        style.configure("Books.Treeview.Heading", font=("Segoe UI", 9, "bold"))

        columns = ("mark", "title", "author", "year", "publisher", "code", "price")
        self.tree = ttk.Treeview(
            self,
            columns=columns,
            show="headings",
            selectmode="browse",
            style="Books.Treeview",
        )
        headings = {
            "mark": "☑",
            "title": "Title",
            "author": "Author",
            "year": "Year",
            "publisher": "Publisher",
            "code": "ISBN / code",
            "price": "Price ₪",
        }
        for key, label in headings.items():
            self.tree.heading(key, text=self._header_text(key, label), command=lambda k=key: self.toggle_sort(k))
            if key == "price":
                anchor = "e"
            elif key in {"mark", "author", "year", "publisher", "code"}:
                anchor = "center"
            else:
                anchor = "w"
            self.tree.column(key, anchor=anchor, stretch=key != "mark", width=80)
        self.tree.column("mark", width=MARK_WIDTH, stretch=False, anchor="center")
        self.tree.column("price", anchor="e")
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Motion>", self._on_motion)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.bind("<Configure>", self._on_resize)

    def _header_text(self, key: str, label: str) -> str:
        if key not in {"mark", "title", "author", "publisher"}:
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

    def select_book(self, book: Book) -> None:
        for iid, item in self._by_iid.items():
            if item is book or item.key() == book.key():
                self.tree.selection_set(iid)
                self.tree.see(iid)
                return

    def toggle_sort(self, column: str) -> None:
        if column not in {"mark", "title", "author", "publisher"}:
            return
        if self.sort_column == column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = column
            self.sort_reverse = False
        headings = {
            "mark": "☑",
            "title": "Title",
            "author": "Author",
            "year": "Year",
            "publisher": "Publisher",
            "code": "ISBN / code",
            "price": "Price ₪",
        }
        for key, label in headings.items():
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
            return title

        self.order.sort(key=value, reverse=self.sort_reverse)

    def selected_books(self) -> list[Book]:
        return [book for book in self.books if book.key() in self.checked]

    def select_all(self) -> None:
        self.checked = {book.key() for book in self.books}
        self._refresh_marks()
        if self.on_check:
            self.on_check()

    def clear_selection(self) -> None:
        self.checked.clear()
        self._refresh_marks()
        if self.on_check:
            self.on_check()

    def _reload(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._by_iid.clear()
        for book_index in self.order:
            book = self.books[book_index]
            key = book.key()
            iid = f"{book_index}:{key}"
            self._by_iid[iid] = book
            self.tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    "☑" if key in self.checked else "☐",
                    book.display_title(),
                    book.author,
                    book.year,
                    book.publisher,
                    book.identity_code(),
                    format_price(book.price_ils),
                ),
            )
        self.after_idle(lambda: self._apply_column_widths(self.winfo_width() or 800))

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
        self.tree.configure(cursor="hand2" if self._column_at(event) == "publisher" else "")

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
            if self.on_check:
                self.on_check()
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
