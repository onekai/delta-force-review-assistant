"""帧率测量：用英特尔开源的 PresentMon 读取 Windows 事件追踪（ETW）里的每一帧呈现时间。

不读游戏内存、不注入；PresentMon 需要管理员权限，所以会弹出一次系统授权窗口（UAC），由你点“是”。
结果：平均帧率、1% Low（= 1000 / 第 99 百分位帧耗时）、99% 帧耗时、逐帧耗时曲线。
"""

from __future__ import annotations

import csv
import ctypes
import logging
import sys
import time
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)
IS_WINDOWS = sys.platform == "win32"
START_DELAY = 10  # 点“是”之后留 10 秒切回游戏，再开始记录


def find_presentmon() -> str:
    bin_dir = paths.app_root() / "bin"
    for p in sorted(bin_dir.glob("PresentMon*.exe"), reverse=True):
        return str(p)
    return ""


def parse_csv(path: Path, process: str = "") -> list[float]:
    """返回每帧耗时（毫秒）。兼容 PresentMon 1.x（MsBetweenPresents）和 2.x（FrameTime / MsBetweenPresents）。"""
    frames: list[float] = []
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames or []
        col = next((c for c in ("FrameTime", "MsBetweenPresents", "msBetweenPresents") if c in cols), None)
        if not col:
            return []
        app_col = next((c for c in ("Application", "ProcessName") if c in cols), None)
        for row in reader:
            if process and app_col and row.get(app_col, "").lower() != process.lower():
                continue
            try:
                value = float(row[col])
            except (TypeError, ValueError):
                continue
            if 0 < value < 1000:
                frames.append(value)
    return frames


def summarize(frames: list[float], points: int = 120) -> dict:
    if len(frames) < 30:
        return {}
    ordered = sorted(frames)
    p99 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))]
    avg_ft = sum(frames) / len(frames)
    # 曲线：分成 points 段，每段取最慢的一帧，卡顿尖峰不会被平均掉
    step = max(1, len(frames) // points)
    series = [round(max(frames[i:i + step]), 2) for i in range(0, len(frames), step)][:points]
    return {"frames": len(frames), "avg": round(1000 / avg_ft, 1), "low1": round(1000 / p99, 1),
            "p99": round(p99, 2), "series": series}


if IS_WINDOWS:
    from ctypes import wintypes

    class _SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                    ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]

    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_SHELLEXECUTEINFOW)]
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _SEE_MASK_NOCLOSEPROCESS = 0x40
    _SEE_MASK_NO_CONSOLE = 0x8000
    _SW_HIDE = 0
    _ERROR_CANCELLED = 1223

    def run_elevated(exe: str, args: str, timeout: float) -> int:
        """以管理员身份运行 PresentMon 并等它结束。用户在授权窗口点“否”时返回 -2。"""
        info = _SHELLEXECUTEINFOW()
        info.cbSize = ctypes.sizeof(info)
        info.fMask = _SEE_MASK_NOCLOSEPROCESS | _SEE_MASK_NO_CONSOLE
        info.lpVerb = "runas"
        info.lpFile = exe
        info.lpParameters = args
        info.nShow = _SW_HIDE
        if not _shell32.ShellExecuteExW(ctypes.byref(info)):
            return -2 if ctypes.get_last_error() == _ERROR_CANCELLED else -1
        try:
            _kernel32.WaitForSingleObject(info.hProcess, int(timeout * 1000))
            code = wintypes.DWORD()
            _kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
            return int(code.value)
        finally:
            _kernel32.CloseHandle(info.hProcess)
else:
    def run_elevated(exe: str, args: str, timeout: float) -> int:
        return -1


class MeasureError(RuntimeError):
    pass


def measure(process_name: str, seconds: int = 60) -> dict:
    exe = find_presentmon()
    if not exe:
        raise MeasureError("没有找到 PresentMon，请重新运行 setup.bat")
    if not IS_WINDOWS:
        raise MeasureError("帧率测量只能在 Windows 上使用")
    out = paths.data_dir() / "perf" / f"presentmon_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    args = (f'--process_name "{process_name}" --output_file "{out}" --delay {START_DELAY} --timed {int(seconds)} '
            f'--terminate_after_timed --stop_existing_session --no_console_stats')
    code = run_elevated(exe, args, timeout=seconds + START_DELAY + 60)
    if code == -2:
        raise MeasureError("你在系统授权窗口点了“否”，没有测量")
    if not out.exists():
        raise MeasureError(f"PresentMon 没有输出数据（退出码 {code}）。请确认游戏在前台运行")
    frames = parse_csv(out, process_name)
    result = summarize(frames)
    if not result:
        raise MeasureError("采到的帧太少：测量期间游戏要一直在前台运行")
    result["seconds"] = seconds
    result["process"] = process_name
    return result
