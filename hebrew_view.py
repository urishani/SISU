"""Windows RTL description box so Hebrew lays out as one right-to-left block."""

from __future__ import annotations

import ctypes
import sys
import tkinter as tk
from ctypes import wintypes

WHITE = "#FFFFFF"

WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
WS_VSCROLL = 0x00200000
ES_MULTILINE = 0x0004
ES_AUTOVSCROLL = 0x0040
ES_READONLY = 0x0800
ES_WANTRETURN = 0x1000
ES_RIGHT = 0x0002

WS_EX_RTLREADING = 0x00002000
WS_EX_RIGHT = 0x00001000
WS_EX_LAYOUTRTL = 0x00400000

WM_SETFONT = 0x0030
DEFAULT_CHARSET = 1
FW_NORMAL = 400
ANTIALIASED_QUALITY = 5

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
]
user32.SetWindowPos.argtypes = [
    wintypes.HWND,
    wintypes.HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
]
user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
gdi32.CreateFontW.restype = wintypes.HFONT
gdi32.GetDeviceCaps.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.LoadLibraryW.restype = wintypes.HMODULE
kernel32.LoadLibraryW.argtypes = [wintypes.LPCWSTR]


def _font_height_px(point_size: int = 10) -> int:
    hdc = user32.GetDC(None)
    try:
        dpi = gdi32.GetDeviceCaps(hdc, 90) or 96
    finally:
        user32.ReleaseDC(None, hdc)
    return -int(point_size * dpi / 72)


def _load_richedit() -> str | None:
    if kernel32.LoadLibraryW("Msftedit.dll"):
        return "RICHEDIT50W"
    if kernel32.LoadLibraryW("riched20.dll"):
        return "RichEdit20W"
    return None


class HebrewDescription(tk.Frame):
    """One read-only RTL text block. Uses a native Windows edit on win32."""

    def __init__(self, parent: tk.Widget, **kwargs) -> None:
        super().__init__(parent, bg=WHITE, highlightthickness=0, **kwargs)
        self._hwnd: int | None = None
        self._font: int | None = None
        self._text = ""
        self._fallback: tk.Text | None = None
        self.bind("<Map>", self._on_map)
        self.bind("<Configure>", self._on_configure)
        self.bind("<Destroy>", self._on_destroy)
        self.after_idle(self._on_map)
        if sys.platform != "win32":
            self._use_fallback()

    def set_text(self, text: str) -> None:
        self._text = text or ""
        if self._hwnd and user32.IsWindow(self._hwnd):
            user32.SetWindowTextW(self._hwnd, self._text)
            return
        if self._fallback is not None:
            widget = self._fallback
            widget.delete("1.0", "end")
            if self._text:
                widget.insert("1.0", "\u2067" + self._text + "\u2069", "rtl")

    def _on_map(self, _event=None) -> None:
        if self._hwnd or self._fallback is not None:
            return
        if sys.platform != "win32" or not self._create_native():
            self._use_fallback()
            self.set_text(self._text)

    def _create_native(self) -> bool:
        self.update_idletasks()
        parent = int(self.winfo_id())
        if not parent:
            return False
        classes = [name for name in (_load_richedit(), "EDIT") if name]
        ex_style = WS_EX_RTLREADING | WS_EX_RIGHT | WS_EX_LAYOUTRTL
        style = (
            WS_CHILD
            | WS_VISIBLE
            | WS_VSCROLL
            | ES_MULTILINE
            | ES_AUTOVSCROLL
            | ES_READONLY
            | ES_WANTRETURN
            | ES_RIGHT
        )
        hwnd = None
        for class_name in classes:
            hwnd = user32.CreateWindowExW(
                ex_style,
                class_name,
                self._text,
                style,
                0,
                0,
                max(self.winfo_width(), 40),
                max(self.winfo_height(), 40),
                parent,
                None,
                kernel32.GetModuleHandleW(None),
                None,
            )
            if hwnd:
                break
        if not hwnd:
            return False
        self._hwnd = int(hwnd)
        self._font = gdi32.CreateFontW(
            _font_height_px(10),
            0,
            0,
            0,
            FW_NORMAL,
            0,
            0,
            0,
            DEFAULT_CHARSET,
            0,
            0,
            ANTIALIASED_QUALITY,
            0,
            "Segoe UI",
        )
        if self._font:
            user32.SendMessageW(self._hwnd, WM_SETFONT, self._font, 1)
        user32.SetWindowTextW(self._hwnd, self._text)
        self._sync_size()
        return True

    def _use_fallback(self) -> None:
        if self._fallback is not None:
            return
        widget = tk.Text(
            self,
            wrap="word",
            font=("Segoe UI", 10),
            bg=WHITE,
            fg="#1B1B1B",
            relief="flat",
            padx=8,
            pady=6,
            undo=False,
        )
        widget.tag_configure("rtl", justify="right")
        widget.pack(fill="both", expand=True)
        self._fallback = widget
        self.set_text(self._text)

    def _sync_size(self) -> None:
        if not self._hwnd or not user32.IsWindow(self._hwnd):
            return
        width = max(self.winfo_width(), 1)
        height = max(self.winfo_height(), 1)
        user32.SetWindowPos(self._hwnd, None, 0, 0, width, height, 0x0014)

    def _on_configure(self, _event=None) -> None:
        self._sync_size()

    def _on_destroy(self, _event=None) -> None:
        hwnd, self._hwnd = self._hwnd, None
        if hwnd and user32.IsWindow(hwnd):
            user32.DestroyWindow(hwnd)
        font, self._font = self._font, None
        if font:
            gdi32.DeleteObject(font)
