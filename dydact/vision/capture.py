"""Screen capture via mss. Returns BGR numpy arrays (what OpenCV expects)."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

import mss
import numpy as np


def _visible_illustrator(monitor: dict, processes=("illustrator.exe",)) -> np.ndarray:
    """Mark visible Illustrator pixels in monitor coordinates, respecting window stacking."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    dwm = ctypes.WinDLL("dwmapi")
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = (callback_type, wintypes.LPARAM)
    user32.IsWindowVisible.argtypes = user32.IsIconic.argtypes = (wintypes.HWND,)
    user32.GetWindowThreadProcessId.argtypes = (
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    )
    user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    dwm.DwmGetWindowAttribute.argtypes = (
        wintypes.HWND,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    process_names = {}
    windows = []
    own_pid = os.getpid()
    left, top = monitor["left"], monitor["top"]
    width, height = monitor["width"], monitor["height"]

    def process_name(pid):
        handle = kernel32.OpenProcess(
            0x1000, False, pid
        )  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            path = ctypes.create_unicode_buffer(32768)
            length = wintypes.DWORD(len(path))
            if kernel32.QueryFullProcessImageNameW(
                handle, 0, path, ctypes.byref(length)
            ):
                return os.path.basename(path.value).casefold()
            return ""
        finally:
            kernel32.CloseHandle(handle)

    @callback_type
    def record_window(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == own_pid:  # The guide and overlay are excluded from the capture.
            return True
        cloaked = wintypes.DWORD()
        dwm.DwmGetWindowAttribute(
            hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
        )
        if cloaked.value:
            return True
        rect = wintypes.RECT()
        if (
            dwm.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect))
            != 0
        ) and not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        x0, y0 = max(0, rect.left - left), max(0, rect.top - top)
        x1, y1 = min(width, rect.right - left), min(height, rect.bottom - top)
        if x0 < x1 and y0 < y1:
            if pid.value not in process_names:
                process_names[pid.value] = process_name(pid.value)
            windows.append(
                (
                    x0,
                    y0,
                    x1,
                    y1,
                    process_names[pid.value] in {p.casefold() for p in processes},
                )
            )
        return True

    user32.EnumWindows(record_window, 0)
    visible = np.zeros((height, width), dtype=bool)
    for x0, y0, x1, y1, illustrator in reversed(windows):
        visible[y0:y1, x0:x1] = illustrator
    return visible


class ScreenCapture:
    def __init__(self, monitor_index: int = 1):
        # mss monitor 0 = all screens combined, 1 = primary, 2+ = others.
        self._sct = mss.mss()
        self.monitor = self._sct.monitors[monitor_index]

    @property
    def size(self) -> tuple[int, int]:
        return self.monitor["width"], self.monitor["height"]

    def grab(self) -> np.ndarray:
        shot = self._sct.grab(self.monitor)
        frame = np.asarray(shot)  # BGRA
        return np.ascontiguousarray(frame[:, :, :3])  # drop alpha -> BGR

    def grab_illustrator(self) -> tuple[np.ndarray, np.ndarray]:
        return self.grab_app(("illustrator.exe",))

    def grab_app(self, processes) -> tuple[np.ndarray, np.ndarray]:
        visible = _visible_illustrator(self.monitor, processes)
        if not visible.any():
            raise RuntimeError(
                "Bring the configured app into view on this monitor and try again."
            )
        frame = self.grab()
        frame[~visible] = 0
        return frame, visible
