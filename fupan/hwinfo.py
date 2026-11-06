"""电脑配置检测（只读）。

数据来源：PowerShell 的系统信息查询（CIM）、注册表、显示设置接口。不装驱动，不读温度。
自动优化（可还原的系统设置修改）在 optimizer.py 里，这里只读。
"""

from __future__ import annotations

import ctypes
import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass

from .encoders import CREATE_NO_WINDOW

log = logging.getLogger(__name__)
IS_WINDOWS = sys.platform == "win32"

_PS_SCRIPT = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ErrorActionPreference = 'SilentlyContinue'
$r = @{}
$r.cpu = @(Get-CimInstance Win32_Processor | Select-Object Name,Manufacturer,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed,L2CacheSize,L3CacheSize,SocketDesignation)
$r.gpu = @(Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion,@{n='DriverDate';e={$_.DriverDate.ToString('yyyy-MM-dd')}},AdapterRAM,PNPDeviceID,VideoProcessor,CurrentHorizontalResolution,CurrentVerticalResolution,CurrentRefreshRate)
$r.ram = @(Get-CimInstance Win32_PhysicalMemory | Select-Object Capacity,Speed,ConfiguredClockSpeed,Manufacturer,PartNumber,DeviceLocator,SMBIOSMemoryType)
$r.memarray = @(Get-CimInstance Win32_PhysicalMemoryArray | Select-Object Use,MemoryDevices,MaxCapacityEx)
$r.board = @(Get-CimInstance Win32_BaseBoard | Select-Object Manufacturer,Product,Version)
$r.bios = @(Get-CimInstance Win32_BIOS | Select-Object Manufacturer,SMBIOSBIOSVersion,@{n='ReleaseDate';e={$_.ReleaseDate.ToString('yyyy-MM-dd')}})
$r.cs = @(Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer,Model,PCSystemType,TotalPhysicalMemory)
$r.os = @(Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,BuildNumber,OSArchitecture)
$r.monitor = @(Get-CimInstance -Namespace root\wmi -ClassName WmiMonitorID | ForEach-Object { [pscustomobject]@{ Name = (($_.UserFriendlyName | Where-Object { $_ -ne 0 } | ForEach-Object { [char]$_ }) -join ''); Maker = (($_.ManufacturerName | Where-Object { $_ -ne 0 } | ForEach-Object { [char]$_ }) -join '') } })
$r.sound = @(Get-CimInstance Win32_SoundDevice | Select-Object Name,Manufacturer)
$r.disk = @(Get-PhysicalDisk | Select-Object FriendlyName,@{n='MediaType';e={"$($_.MediaType)"}},@{n='BusType';e={"$($_.BusType)"}},Size)
$r.volume = @(Get-Volume | Where-Object DriveLetter | Select-Object @{n='DriveLetter';e={"$($_.DriveLetter)"}},SizeRemaining,Size)
$r.power = ((powercfg /getactivescheme) -join ' ')
$r.battery = @(Get-CimInstance Win32_Battery | Select-Object BatteryStatus,EstimatedChargeRemaining)
$r | ConvertTo-Json -Depth 4 -Compress
"""


def _run_powershell() -> dict:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _PS_SCRIPT],
            capture_output=True, timeout=60, creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("读取系统信息失败：%s", exc)
        return {}
    text = out.stdout.decode("utf-8", "replace").strip().lstrip("﻿")
    try:
        return json.loads(text) if text else {}
    except ValueError:
        log.warning("系统信息解析失败：%s", text[:200])
        return {}


def _reg_value(root, path: str, name: str):
    import winreg
    try:
        with winreg.OpenKey(root, path) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def _registry_info() -> dict:
    import winreg
    hkcu, hklm = winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE
    info = {
        "game_mode": _reg_value(hkcu, r"Software\Microsoft\GameBar", "AutoGameModeEnabled"),
        "hags": _reg_value(hklm, r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers", "HwSchMode"),
        "gamedvr_enabled": _reg_value(hkcu, r"System\GameConfigStore", "GameDVR_Enabled"),
        "background_capture": _reg_value(hkcu, r"Software\Microsoft\Windows\CurrentVersion\GameDVR", "HistoricalCaptureEnabled"),
        "vram": [],
    }
    base = r"SYSTEM\ControlSet001\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    for i in range(10):
        sub = f"{base}\\{i:04d}"
        size = _reg_value(hklm, sub, "HardwareInformation.qwMemorySize")
        desc = _reg_value(hklm, sub, "DriverDesc")
        if desc and size:
            info["vram"].append({"name": desc, "bytes": int(size)})
    return info


if IS_WINDOWS:
    from ctypes import wintypes

    class _DEVMODEW(ctypes.Structure):
        _fields_ = [
            ("dmDeviceName", wintypes.WCHAR * 32), ("dmSpecVersion", wintypes.WORD),
            ("dmDriverVersion", wintypes.WORD), ("dmSize", wintypes.WORD), ("dmDriverExtra", wintypes.WORD),
            ("dmFields", wintypes.DWORD), ("dmPositionX", wintypes.LONG), ("dmPositionY", wintypes.LONG),
            ("dmDisplayOrientation", wintypes.DWORD), ("dmDisplayFixedOutput", wintypes.DWORD),
            ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short), ("dmYResolution", ctypes.c_short),
            ("dmTTOption", ctypes.c_short), ("dmCollate", ctypes.c_short), ("dmFormName", wintypes.WCHAR * 32),
            ("dmLogPixels", wintypes.WORD), ("dmBitsPerPel", wintypes.DWORD), ("dmPelsWidth", wintypes.DWORD),
            ("dmPelsHeight", wintypes.DWORD), ("dmDisplayFlags", wintypes.DWORD),
            ("dmDisplayFrequency", wintypes.DWORD), ("dmICMMethod", wintypes.DWORD),
            ("dmICMIntent", wintypes.DWORD), ("dmMediaType", wintypes.DWORD), ("dmDitherType", wintypes.DWORD),
            ("dmReserved1", wintypes.DWORD), ("dmReserved2", wintypes.DWORD),
            ("dmPanningWidth", wintypes.DWORD), ("dmPanningHeight", wintypes.DWORD),
        ]

    _user32 = ctypes.WinDLL("user32")
    _user32.EnumDisplaySettingsW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(_DEVMODEW)]
    _user32.ChangeDisplaySettingsExW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_DEVMODEW), wintypes.HWND,
                                                 wintypes.DWORD, ctypes.c_void_p]
    _user32.ChangeDisplaySettingsExW.restype = ctypes.c_long
    _ENUM_CURRENT = 0xFFFFFFFF
    _DM_INTERLACED = 0x2
    _DM_DISPLAYFREQUENCY = 0x400000
    _CDS_UPDATEREGISTRY = 0x1
    _CDS_TEST = 0x2
    _DISP_RESULTS = {-1: "显卡驱动拒绝了这个模式", -2: "显示器不支持这个模式", -3: "无法写入设置",
                     -4: "参数错误", -5: "参数错误", 1: "需要重启电脑才生效"}

    def _current_mode(device: str):
        cur = _DEVMODEW()
        cur.dmSize = ctypes.sizeof(_DEVMODEW)
        return cur if _user32.EnumDisplaySettingsW(device, _ENUM_CURRENT, ctypes.byref(cur)) else None

    def display_modes() -> list[dict]:
        """每个显示器：当前分辨率、当前刷新率、同分辨率同色深下支持的最高刷新率（不算隔行模式）。"""
        from .winapi import list_monitors
        result = []
        for mon in list_monitors():
            cur = _current_mode(mon.device)
            if cur is None:
                continue
            best, i = cur.dmDisplayFrequency, 0
            mode = _DEVMODEW()
            mode.dmSize = ctypes.sizeof(_DEVMODEW)
            while _user32.EnumDisplaySettingsW(mon.device, i, ctypes.byref(mode)):
                if mode.dmPelsWidth == cur.dmPelsWidth and mode.dmPelsHeight == cur.dmPelsHeight \
                        and mode.dmBitsPerPel == cur.dmBitsPerPel and not (mode.dmDisplayFlags & _DM_INTERLACED):
                    best = max(best, mode.dmDisplayFrequency)
                i += 1
            result.append({"index": mon.index, "device": mon.device, "primary": mon.primary,
                           "width": cur.dmPelsWidth, "height": cur.dmPelsHeight,
                           "hz": cur.dmDisplayFrequency, "max_hz": best})
        return result

    def change_refresh(device: str, hz: int) -> tuple[bool, str]:
        """只改刷新率，分辨率不变。先用 CDS_TEST 试一次，系统确认支持才真正修改。"""
        mode = _current_mode(device)
        if mode is None:
            return False, "读不到显示器当前设置"
        mode.dmDisplayFrequency = int(hz)
        mode.dmFields = _DM_DISPLAYFREQUENCY
        test = _user32.ChangeDisplaySettingsExW(device, ctypes.byref(mode), None, _CDS_TEST, None)
        if test != 0:
            return False, _DISP_RESULTS.get(test, f"错误码 {test}")
        result = _user32.ChangeDisplaySettingsExW(device, ctypes.byref(mode), None, _CDS_UPDATEREGISTRY, None)
        return result == 0, "" if result == 0 else _DISP_RESULTS.get(result, f"错误码 {result}")

    _display_modes = display_modes
else:
    def display_modes() -> list[dict]:
        return []

    def change_refresh(device: str, hz: int) -> tuple[bool, str]:
        return False, "只在 Windows 上可用"

    _display_modes = display_modes


_LIST_KEYS = ("cpu", "gpu", "ram", "os", "disk", "volume", "battery", "memarray", "board", "bios", "cs", "monitor",
              "sound")


def normalize(raw: dict) -> dict:
    """PowerShell 只有一项结果时可能返回单个对象，这里统一成列表。"""
    out = dict(raw)
    for key in _LIST_KEYS:
        value = out.get(key)
        if value is None:
            out[key] = []
        elif isinstance(value, dict):
            out[key] = [value]
    return out


def collect() -> dict:
    """在后台线程里调用，大约需要 2～10 秒。"""
    info: dict = {"collected_at": time.time(), "platform": sys.platform}
    if not IS_WINDOWS:
        return info
    info.update(normalize(_run_powershell()))
    try:
        info["registry"] = _registry_info()
    except OSError as exc:
        info["registry"] = {}
        log.warning("读取注册表失败：%s", exc)
    info["displays"] = _display_modes()
    return info


def gpu_names(info: dict) -> list[str]:
    return [g.get("Name", "") for g in info.get("gpu", []) if g.get("Name")]


def ram_summary(info: dict) -> dict | None:
    ram = info.get("ram") or []
    if not ram:
        return None
    total = sum(_gb(r.get("Capacity")) for r in ram)
    speed = max(int(r.get("ConfiguredClockSpeed") or r.get("Speed") or 0) for r in ram)
    kind = {26: "DDR4", 34: "DDR5", 24: "DDR3"}.get(int(ram[0].get("SMBIOSMemoryType") or 0), "")
    return {"total_gb": total, "speed": speed, "kind": kind, "sticks": len(ram)}


@dataclass(frozen=True)
class Finding:
    level: str  # ok / warn / info
    item: str
    detail: str


def _gb(value) -> float:
    try:
        return float(value) / 1024 ** 3
    except (TypeError, ValueError):
        return 0.0


def summarize(info: dict) -> list[tuple[str, str]]:
    """首页显示的配置清单：(项目, 内容)。"""
    rows: list[tuple[str, str]] = []
    for cpu in info.get("cpu", []):
        rows.append(("CPU", f"{cpu.get('Name', '').strip()}（{cpu.get('NumberOfCores')} 核 {cpu.get('NumberOfLogicalProcessors')} 线程）"))
    vram = {v["name"]: v["bytes"] for v in (info.get("registry") or {}).get("vram", [])}
    for gpu in info.get("gpu", []):
        name = gpu.get("Name", "")
        mem = vram.get(name) or gpu.get("AdapterRAM") or 0
        rows.append(("显卡", f"{name}（显存 {_gb(mem):.0f} GB，驱动 {gpu.get('DriverVersion', '')}，{gpu.get('DriverDate', '')}）"))
    ram = info.get("ram", [])
    if ram:
        total = sum(_gb(r.get("Capacity")) for r in ram)
        speed = max(int(r.get("ConfiguredClockSpeed") or 0) for r in ram)
        kind = {26: "DDR4", 34: "DDR5"}.get(int(ram[0].get("SMBIOSMemoryType") or 0), "")
        rows.append(("内存", f"{total:.0f} GB {kind}，{len(ram)} 根，运行频率 {speed} MT/s"))
    for disk in info.get("disk", []):
        rows.append(("硬盘", f"{disk.get('FriendlyName', '')}（{disk.get('MediaType', '')}，{disk.get('BusType', '')}，{_gb(disk.get('Size')):.0f} GB）"))
    for d in info.get("displays", []):
        rows.append(("显示器", f"{'主屏 ' if d.get('primary') else ''}{d['width']}×{d['height']}，当前 {d['hz']} Hz，最高 {d['max_hz']} Hz"))
    for os_ in info.get("os", []):
        rows.append(("系统", f"{os_.get('Caption', '')} {os_.get('Version', '')}"))
    if info.get("power"):
        rows.append(("电源计划", _power_plan_name(info["power"])))
    return rows


def _power_plan_name(text: str) -> str:
    if "(" in text and ")" in text:
        return text[text.rfind("(") + 1:text.rfind(")")]
    return text.strip()


def analyze(info: dict, recordings_free_gb: float | None = None) -> list[Finding]:
    found: list[Finding] = []
    for d in info.get("displays", []):
        if d.get("max_hz", 0) > d.get("hz", 0):
            found.append(Finding("warn", "显示器刷新率", f"显示器 {d['index'] + 1} 当前 {d['hz']} Hz，最高支持 {d['max_hz']} Hz。高刷屏跑在低刷新率会让画面更卡"))
        elif d.get("hz"):
            found.append(Finding("ok", "显示器刷新率", f"显示器 {d['index'] + 1} 已在最高 {d['hz']} Hz"))

    plan = _power_plan_name(info.get("power", ""))
    low = plan.lower()
    if plan:
        if "节能" in plan or "saver" in low:
            found.append(Finding("warn", "电源计划", f"当前是“{plan}”，会限制 CPU 性能，建议改为高性能"))
        elif "平衡" in plan or "balanced" in low:
            found.append(Finding("info", "电源计划", f"当前是“{plan}”，改为高性能通常能提高 1% low 帧率"))
        else:
            found.append(Finding("ok", "电源计划", f"当前是“{plan}”"))

    reg = info.get("registry") or {}
    if reg.get("game_mode") == 0:
        found.append(Finding("warn", "游戏模式", "Windows 游戏模式已关闭，建议打开"))
    elif reg:
        found.append(Finding("ok", "游戏模式", "已开启"))
    if reg and reg.get("hags") != 2:
        found.append(Finding("info", "硬件加速 GPU 计划", "未开启。开启后需要重启，部分显卡上能降低延迟"))
    if reg.get("background_capture") == 1:
        found.append(Finding("warn", "后台录制", "Xbox Game Bar 后台录制已开启，会和本软件重复录制、占用性能，建议关闭"))

    ram = info.get("ram", [])
    if ram:
        total = sum(_gb(r.get("Capacity")) for r in ram)
        if total < 15.5:
            found.append(Finding("warn", "内存容量", f"内存 {total:.0f} GB，低于 16 GB，大地图容易卡顿"))
        if len(ram) == 1:
            found.append(Finding("info", "内存通道", "只有 1 根内存，是单通道，换成 2 根组双通道能明显提升帧率"))
        kind = int(ram[0].get("SMBIOSMemoryType") or 0)
        speed = max(int(r.get("ConfiguredClockSpeed") or 0) for r in ram)
        if (kind == 26 and 0 < speed < 3000) or (kind == 34 and 0 < speed < 4800):
            found.append(Finding("info", "内存频率", f"内存运行在 {speed} MT/s，可能没有开启 XMP/EXPO，需要在 BIOS 里打开"))

    for bat in info.get("battery", []):
        if bat.get("BatteryStatus") in (1, 4, 5):  # 1 放电中，4 电量低，5 电量极低
            found.append(Finding("warn", "笔记本电源", "笔记本没有插电，显卡会降频，请插上电源再玩"))

    names = [n.lower() for n in gpu_names(info)]
    has_dgpu = any(("nvidia" in n or "geforce" in n or "radeon rx" in n or "arc a" in n) for n in names)
    has_igpu = any(("intel" in n and "arc a" not in n) or "radeon(tm) graphics" in n for n in names)
    if has_dgpu and has_igpu:
        found.append(Finding("info", "双显卡", "检测到独显和核显，需要确认游戏跑在独显上（下一版会自动设置）"))

    if recordings_free_gb is not None and recordings_free_gb < 50:
        found.append(Finding("warn", "录像硬盘空间", f"录像所在硬盘只剩 {recordings_free_gb:.0f} GB，1 小时录像约 9 GB"))
    return found


# ---- 电脑完整配置（给“赛前优化”页面和首页用；只读，不含序列号、电脑名、用户名）----
_MEM_TYPES = {20: "DDR", 21: "DDR2", 24: "DDR3", 26: "DDR4", 34: "DDR5", 35: "LPDDR5"}
_MONITOR_MAKERS = {"ACR": "宏碁", "AOC": "AOC", "AUS": "华硕", "BNQ": "明基", "DEL": "戴尔", "GSM": "LG", "HKC": "HKC",
                   "HPN": "惠普", "HWP": "惠普", "KTC": "KTC", "LEN": "联想", "MSI": "微星", "PHL": "飞利浦",
                   "SAM": "三星", "SNY": "索尼", "XMI": "小米", "HSD": "HKC", "TCL": "TCL", "GBT": "技嘉", "SKG": "SKG"}
_PLACEHOLDERS = ("to be filled", "system product name", "system manufacturer", "default string", "o.e.m.", "not applicable")


def _clean(text) -> str:
    t = " ".join(str(text or "").replace("(R)", "").replace("(TM)", "").replace("(tm)", "").split())
    return "" if any(p in t.lower() for p in _PLACEHOLDERS) else t


def _is_dgpu(name: str) -> bool:
    n = name.lower()
    return any(k in n for k in ("nvidia", "geforce", "radeon rx", "radeon pro", "arc a", "arc b"))


def _ghz(mhz) -> str:
    try:
        return f"{float(mhz) / 1000:.1f} GHz" if float(mhz) > 0 else ""
    except (TypeError, ValueError):
        return ""


def _mb(kb) -> str:
    try:
        v = float(kb) / 1024
    except (TypeError, ValueError):
        return ""
    return f"{v:.0f} MB" if v >= 1 else ""


def _slots(info: dict) -> int:
    arrays = info.get("memarray") or []
    used = [a for a in arrays if a.get("Use") in (3, None)] or arrays
    return sum(int(a.get("MemoryDevices") or 0) for a in used)


def _gpus(info: dict) -> list[dict]:
    vram = {v["name"]: v["bytes"] for v in (info.get("registry") or {}).get("vram", [])}
    out = []
    for g in info.get("gpu", []):
        name = _clean(g.get("Name"))
        if not name or "basic display" in name.lower() or "remote" in name.lower():
            continue
        out.append({"name": name, "dgpu": _is_dgpu(name), "vram": _gb(vram.get(g.get("Name")) or g.get("AdapterRAM") or 0),
                    "driver": g.get("DriverVersion") or "", "date": g.get("DriverDate") or ""})
    out.sort(key=lambda x: not x["dgpu"])
    return out


def _short_cpu(name: str) -> str:
    for junk in (" Processor", " CPU"):
        name = name.replace(junk, "")
    name = " ".join(w for w in name.split() if not w.endswith("-Core"))
    return name.split(" @ ")[0].strip()


def _short_gpu(name: str) -> str:
    for junk in ("NVIDIA GeForce ", "NVIDIA ", "AMD Radeon ", "Intel(R) ", "Intel "):
        if name.startswith(junk):
            return ("Radeon " if junk == "AMD Radeon " else "") + name[len(junk):]
    return name


def spec_groups(info: dict) -> list[dict]:
    """完整配置，分组显示：[{"group", "icon", "rows": [{"k", "v"}]}]。读不到的项不显示。"""
    groups = []

    def add(group: str, icon: str, rows: list[tuple[str, str]]):
        rows = [{"k": k, "v": v} for k, v in rows if v]
        if rows:
            groups.append({"group": group, "icon": icon, "rows": rows})

    rows = []
    for i, cpu in enumerate(info.get("cpu", []), 1):
        pre = f"CPU {i} " if len(info.get("cpu", [])) > 1 else ""
        rows.append((pre + "型号", _clean(cpu.get("Name"))))
        if cpu.get("NumberOfCores"):
            rows.append((pre + "核心 / 线程", f"{cpu.get('NumberOfCores')} 核 {cpu.get('NumberOfLogicalProcessors')} 线程"))
        rows.append((pre + "基础频率", _ghz(cpu.get("MaxClockSpeed"))))
        cache = " · ".join(x for x in (f"L2 {_mb(cpu.get('L2CacheSize'))}" if _mb(cpu.get("L2CacheSize")) else "",
                                        f"L3 {_mb(cpu.get('L3CacheSize'))}" if _mb(cpu.get("L3CacheSize")) else "") if x)
        rows.append((pre + "缓存", cache))
        rows.append((pre + "插槽", _clean(cpu.get("SocketDesignation"))))
    add("处理器", "memory", rows)

    rows = []
    gpus = _gpus(info)
    for i, g in enumerate(gpus, 1):
        kind = "独立显卡" if g["dgpu"] else "核显"
        mem = f"，显存 {g['vram']:.0f} GB" if g["vram"] >= 1 else ""
        rows.append((f"显卡 {i}" if len(gpus) > 1 else "型号", f"{g['name']}（{kind}{mem}）"))
        rows.append((f"显卡 {i} 驱动" if len(gpus) > 1 else "驱动", " · ".join(x for x in (g["driver"], g["date"]) if x)))
    add("显卡", "developer_board", rows)

    rows = []
    ram = info.get("ram", [])
    if ram:
        total = sum(_gb(r.get("Capacity")) for r in ram)
        kind = _MEM_TYPES.get(int(ram[0].get("SMBIOSMemoryType") or 0), "")
        speed = max(int(r.get("ConfiguredClockSpeed") or r.get("Speed") or 0) for r in ram)
        channel = "单通道" if len(ram) == 1 else "双通道" if len(ram) in (2, 4) else f"{len(ram)} 根"
        rows.append(("总容量", f"{total:.0f} GB {kind} · {len(ram)} 根 · {channel}".replace("  ", " ")))
        if speed:
            rows.append(("运行频率", f"{speed} MT/s"))
        slots = _slots(info)
        if slots:
            free = max(0, slots - len(ram))
            tip = "（再插 1 根同型号内存就是双通道）" if len(ram) == 1 and free else ""
            rows.append(("插槽", f"已用 {len(ram)} / 共 {slots} 个{tip}"))
        for r in ram:
            maker = _clean(r.get("Manufacturer"))
            maker = "" if maker.lower() in ("unknown", "0000", "") else maker
            desc = " ".join(x for x in (f"{_gb(r.get('Capacity')):.0f} GB", maker, _clean(r.get("PartNumber"))) if x)
            rated = int(r.get("Speed") or 0)
            rows.append((_clean(r.get("DeviceLocator")) or "内存条", desc + (f" · 额定 {rated} MT/s" if rated else "")))
    add("内存", "memory_alt", rows)

    rows = []
    for b in info.get("board", []):
        rows.append(("型号", " ".join(x for x in (_clean(b.get("Manufacturer")), _clean(b.get("Product"))) if x)))
    for b in info.get("bios", []):
        ver = " ".join(x for x in (_clean(b.get("Manufacturer")), _clean(b.get("SMBIOSBIOSVersion"))) if x)
        rows.append(("BIOS", ver + (f"（{b['ReleaseDate']}）" if b.get("ReleaseDate") else "")))
    add("主板", "developer_board", rows)

    rows = []
    for d in info.get("disk", []):
        media_type = {"SSD": "固态硬盘", "HDD": "机械硬盘"}.get(str(d.get("MediaType") or ""), "")
        bus = str(d.get("BusType") or "")
        meta = " · ".join(x for x in (media_type, bus if bus not in ("", "Unknown") else "", f"{_gb(d.get('Size')):.0f} GB") if x)
        rows.append((_clean(d.get("FriendlyName")) or "硬盘", meta))
    for v in info.get("volume", []):
        size = _gb(v.get("Size"))
        if size >= 1:
            rows.append((f"{v.get('DriveLetter')} 盘", f"可用 {_gb(v.get('SizeRemaining')):.0f} GB / 共 {size:.0f} GB"))
    add("硬盘", "hard_drive", rows)

    rows = []
    for d in info.get("displays", []):
        rows.append((f"显示器 {d['index'] + 1}" + ("（主屏）" if d.get("primary") else ""),
                     f"{d['width']}×{d['height']} · 当前 {d['hz']} Hz · 最高 {d['max_hz']} Hz"))
    names = []
    for m in info.get("monitor", []):
        name = _clean(m.get("Name"))
        maker = _MONITOR_MAKERS.get(str(m.get("Maker") or "").upper(), _clean(m.get("Maker")))
        if name or maker:
            names.append(" ".join(x for x in (maker, name) if x))
    if names:
        rows.append(("型号", "，".join(names)))
    add("显示器", "desktop_windows", rows)

    sound = list(dict.fromkeys(_clean(x.get("Name")) for x in info.get("sound", []) if _clean(x.get("Name"))))
    add("声音设备", "headphones", [(f"设备 {i}", n) for i, n in enumerate(sound, 1)])

    rows = []
    for o in info.get("os", []):
        arch = "64 位" if "64" in str(o.get("OSArchitecture") or "") else str(o.get("OSArchitecture") or "")
        rows.append(("系统", f"{_clean(o.get('Caption'))}（版本 {o.get('Version', '')}{'，' + arch if arch else ''}）"))
    for c in info.get("cs", []):
        kind = {1: "台式机", 2: "笔记本", 3: "工作站"}.get(int(c.get("PCSystemType") or 0), "")
        rows.append(("电脑类型", kind))
        rows.append(("品牌型号", " ".join(x for x in (_clean(c.get("Manufacturer")), _clean(c.get("Model"))) if x)))
    if info.get("power"):
        rows.append(("电源计划", _power_plan_name(info["power"])))
    add("系统", "settings", rows)
    return groups


def spec_summary(info: dict) -> str:
    """一行概括：CPU · 显卡 · 内存 · 显示器。"""
    parts = []
    cpu = (info.get("cpu") or [{}])[0]
    if cpu.get("Name"):
        parts.append(_short_cpu(_clean(cpu["Name"])))
    gpus = _gpus(info)
    if gpus:
        g = gpus[0]
        parts.append(_short_gpu(g["name"]) + (f" {g['vram']:.0f}GB" if g["vram"] >= 1 and "GB" not in g["name"] else ""))
    ram = ram_summary(info)
    if ram:
        parts.append(f"{ram['total_gb']:.0f}GB {ram['kind']} {'单通道' if ram['sticks'] == 1 else '双通道'}".replace("  ", " "))
    disp = next((d for d in info.get("displays", []) if d.get("primary")), None) or (info.get("displays") or [None])[0]
    if disp:
        parts.append(f"{disp['width']}×{disp['height']} {disp['max_hz']}Hz")
    return " · ".join(parts)
