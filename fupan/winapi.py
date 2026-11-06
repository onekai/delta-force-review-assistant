"""Windows 系统接口（ctypes）。

只做这些事：枚举顶层窗口、读窗口标题和位置、取前台窗口、枚举显示器、用系统快照列出进程名。
不调用 OpenProcess，不读任何进程的内存。非 Windows 系统上所有函数返回空结果，方便在开发机上跑测试。
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass

IS_WINDOWS = sys.platform == "win32"


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    pid: int
    exe: str
    rect: Rect
    visible: bool
    minimized: bool


@dataclass(frozen=True)
class MonitorInfo:
    index: int
    rect: Rect
    primary: bool
    device: str


if IS_WINDOWS:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    dwmapi = ctypes.WinDLL("dwmapi")

    class _RECT(ctypes.Structure):
        _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                    ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    class _MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", _RECT), ("rcWork", _RECT),
                    ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    _EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _MonitorEnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                          ctypes.POINTER(_RECT), wintypes.LPARAM)

    user32.EnumWindows.argtypes = [_EnumWindowsProc, wintypes.LPARAM]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.MonitorFromWindow.restype = wintypes.HMONITOR
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MONITORINFOEXW)]
    user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(_RECT), _MonitorEnumProc, wintypes.LPARAM]
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]

    _TH32CS_SNAPPROCESS = 0x00000002
    _INVALID_HANDLE = ctypes.c_void_p(-1).value
    _DWMWA_EXTENDED_FRAME_BOUNDS = 9
    _MONITOR_DEFAULTTONEAREST = 2
    _MONITORINFOF_PRIMARY = 1
    _GA_ROOT = 2

    def process_names() -> dict[int, str]:
        """pid -> exe 名。用系统快照读取，不打开任何进程。"""
        snap = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if not snap or snap == _INVALID_HANDLE:
            return {}
        result: dict[int, str] = {}
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                result[int(entry.th32ProcessID)] = entry.szExeFile
                ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snap)
        return result

    def _window_rect(hwnd) -> Rect:
        rect = _RECT()
        if dwmapi.DwmGetWindowAttribute(hwnd, _DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)) != 0:
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
        return Rect(rect.left, rect.top, rect.right, rect.bottom)

    def list_windows() -> list[WindowInfo]:
        names = process_names()
        windows: list[WindowInfo] = []

        def callback(hwnd, _lparam):
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            windows.append(WindowInfo(
                hwnd=int(hwnd or 0),
                title=buf.value,
                pid=int(pid.value),
                exe=names.get(int(pid.value), ""),
                rect=_window_rect(hwnd),
                visible=bool(user32.IsWindowVisible(hwnd)),
                minimized=bool(user32.IsIconic(hwnd)),
            ))
            return True

        user32.EnumWindows(_EnumWindowsProc(callback), 0)
        return windows

    def foreground_hwnd() -> int:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return 0
        root = user32.GetAncestor(hwnd, _GA_ROOT)
        return int(root or hwnd)

    def list_monitors() -> list[MonitorInfo]:
        """主显示器排第一，其余按系统枚举顺序。录制用的显示器编号按这个顺序估计。"""
        found: list[tuple[Rect, bool, str]] = []

        def callback(hmon, _hdc, _rect, _lparam):
            info = _MONITORINFOEXW()
            info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
            if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                r = info.rcMonitor
                found.append((Rect(r.left, r.top, r.right, r.bottom),
                              bool(info.dwFlags & _MONITORINFOF_PRIMARY), info.szDevice))
            return True

        user32.EnumDisplayMonitors(None, None, _MonitorEnumProc(callback), 0)
        found.sort(key=lambda item: not item[1])
        return [MonitorInfo(i, rect, primary, device) for i, (rect, primary, device) in enumerate(found)]

else:
    def process_names() -> dict[int, str]:
        return {}

    def list_windows() -> list[WindowInfo]:
        return []

    def foreground_hwnd() -> int:
        return 0

    def list_monitors() -> list[MonitorInfo]:
        return []


def monitor_for_rect(rect: Rect, monitors: list[MonitorInfo]) -> MonitorInfo | None:
    """窗口中心点落在哪个显示器上。"""
    cx = (rect.left + rect.right) // 2
    cy = (rect.top + rect.bottom) // 2
    for mon in monitors:
        if mon.rect.left <= cx < mon.rect.right and mon.rect.top <= cy < mon.rect.bottom:
            return mon
    return monitors[0] if monitors else None
