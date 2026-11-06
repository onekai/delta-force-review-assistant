"""显卡温度和显存：只支持 NVIDIA（通过驱动自带的 nvml.dll）。AMD / Intel 没有简单可靠的公开接口，界面显示“仅 NVIDIA 可读”。"""

from __future__ import annotations

import ctypes
import logging
import sys
import time

log = logging.getLogger(__name__)


class _Memory(ctypes.Structure):
    _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]


class NvidiaStats:
    def __init__(self):
        self._lib = None
        self._handle = None
        self._name = ""
        self._last: dict | None = None
        self._last_at = 0.0
        self._failed = sys.platform != "win32"

    def _init(self) -> bool:
        if self._failed:
            return False
        if self._lib is not None:
            return True
        try:
            lib = ctypes.WinDLL("nvml.dll")
        except OSError:
            try:
                lib = ctypes.WinDLL(r"C:\Program Files\NVIDIA Corporation\NVSMI\nvml.dll")
            except OSError:
                self._failed = True
                return False
        try:
            if lib.nvmlInit_v2() != 0:
                raise OSError("nvmlInit failed")
            handle = ctypes.c_void_p()
            if lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(handle)) != 0:
                raise OSError("no device")
            name = ctypes.create_string_buffer(96)
            lib.nvmlDeviceGetName(handle, name, 96)
            self._lib, self._handle, self._name = lib, handle, name.value.decode("utf-8", "replace")
            return True
        except (OSError, AttributeError) as exc:
            log.info("读不到 NVIDIA 显卡状态：%s", exc)
            self._failed = True
            return False

    def read(self, max_age: float = 5.0) -> dict | None:
        if time.time() - self._last_at < max_age:
            return self._last
        self._last_at = time.time()
        if not self._init():
            self._last = None
            return None
        temp = ctypes.c_uint()
        mem = _Memory()
        try:
            t_ok = self._lib.nvmlDeviceGetTemperature(self._handle, 0, ctypes.byref(temp)) == 0
            m_ok = self._lib.nvmlDeviceGetMemoryInfo(self._handle, ctypes.byref(mem)) == 0
        except OSError:
            self._last = None
            return None
        self._last = {"name": self._name, "temp_c": int(temp.value) if t_ok else None,
                      "vram_used_mb": int(mem.used // 1024 ** 2) if m_ok else None,
                      "vram_total_mb": int(mem.total // 1024 ** 2) if m_ok else None}
        return self._last
