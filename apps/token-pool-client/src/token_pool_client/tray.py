from __future__ import annotations

import ctypes
import sys
import threading
from pathlib import Path
from typing import Callable


class NotificationTray:
    """Small dependency-free Windows notification-area icon."""

    def __init__(
        self,
        icon_path: Path,
        *,
        on_show: Callable[[], None],
        on_refresh: Callable[[], None],
        on_update: Callable[[], None],
        on_exit: Callable[[], None],
    ) -> None:
        self.icon_path = icon_path
        self.callbacks = {
            1001: on_show,
            1002: on_refresh,
            1003: on_update,
            1004: on_exit,
        }
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._hwnd = 0
        self._nid = None
        self._wndproc = None
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return sys.platform == "win32" and self._hwnd != 0

    def start(self) -> bool:
        if sys.platform != "win32":
            return False
        self._thread = threading.Thread(target=self._message_loop, name="token-pool-tray", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)
        return self.available

    def notify(self, title: str, message: str) -> None:
        if not self.available or self._nid is None:
            return
        with self._lock:
            self._nid.uFlags = 0x10
            self._nid.szInfoTitle = title[:63]
            self._nid.szInfo = message[:255]
            self._nid.dwInfoFlags = 0x1
            ctypes.windll.shell32.Shell_NotifyIconW(0x1, ctypes.byref(self._nid))

    def stop(self) -> None:
        if self.available:
            ctypes.windll.user32.PostMessageW(self._hwnd, 0x0010, 0, 0)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)

    def _message_loop(self) -> None:
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32
        kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
        user32.CreatePopupMenu.restype = wintypes.HMENU
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadIconW.restype = wintypes.HICON
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        WM_APP_TRAY = 0x8000 + 71
        WM_DESTROY = 0x0002
        WM_CLOSE = 0x0010
        WM_LBUTTONUP = 0x0202
        WM_LBUTTONDBLCLK = 0x0203
        WM_RBUTTONUP = 0x0205

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256),
                ("uVersion", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", GUID),
                ("hBalloonIcon", wintypes.HICON),
            ]

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
            ctypes.c_void_p,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND

        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

        def dispatch(command: int) -> None:
            callback = self.callbacks.get(command)
            if callback is not None:
                threading.Thread(target=callback, name=f"tray-command-{command}", daemon=True).start()

        def show_menu(hwnd: int) -> None:
            menu = user32.CreatePopupMenu()
            try:
                user32.AppendMenuW(menu, 0, 1001, "Open Token Pool Client")
                user32.AppendMenuW(menu, 0, 1002, "Renew work account now")
                user32.AppendMenuW(menu, 0, 1003, "Check for updates")
                user32.AppendMenuW(menu, 0x800, 0, None)
                user32.AppendMenuW(menu, 0, 1004, "Exit")
                point = wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(point))
                user32.SetForegroundWindow(hwnd)
                command = user32.TrackPopupMenu(menu, 0x0100 | 0x0002, point.x, point.y, 0, hwnd, None)
                if command:
                    dispatch(int(command))
            finally:
                user32.DestroyMenu(menu)

        @WNDPROC
        def wndproc(hwnd, message, wparam, lparam):
            if message == WM_APP_TRAY:
                event = int(lparam) & 0xFFFF
                if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    dispatch(1001)
                elif event == WM_RBUTTONUP:
                    show_menu(hwnd)
                return 0
            if message == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            if message == WM_DESTROY:
                if self._nid is not None:
                    shell32.Shell_NotifyIconW(0x2, ctypes.byref(self._nid))
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)

        self._wndproc = wndproc
        instance = kernel32.GetModuleHandleW(None)
        class_name = f"FMBSMTokenPoolTray{ctypes.windll.kernel32.GetCurrentProcessId()}"
        window_class = WNDCLASSW()
        window_class.lpfnWndProc = ctypes.cast(wndproc, ctypes.c_void_p).value
        window_class.hInstance = instance
        window_class.lpszClassName = class_name
        atom = user32.RegisterClassW(ctypes.byref(window_class))
        if not atom:
            self._ready.set()
            return
        hwnd = user32.CreateWindowExW(0, class_name, class_name, 0, 0, 0, 0, 0, 0, 0, instance, None)
        if not hwnd:
            self._ready.set()
            return
        self._hwnd = hwnd
        icon = user32.LoadImageW(None, str(self.icon_path), 1, 0, 0, 0x0010)
        if not icon:
            icon = user32.LoadIconW(None, 32512)
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = 0x1 | 0x2 | 0x4
        nid.uCallbackMessage = WM_APP_TRAY
        nid.hIcon = icon
        nid.szTip = "FMBSM Token Pool Client"
        self._nid = nid
        if not shell32.Shell_NotifyIconW(0x0, ctypes.byref(nid)):
            self._hwnd = 0
            self._ready.set()
            user32.DestroyWindow(hwnd)
            return
        self._ready.set()
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), 0, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
        self._hwnd = 0
