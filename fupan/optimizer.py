"""赛前优化：只改公开、可还原的 Windows 设置，每项改动先把原值存进本地数据库，随时一键还原。

会自动处理的（需要你开启“赛前自动优化”）：
- 显示器刷新率调到当前分辨率支持的最高值（只在游戏没运行时改，避免游戏画面闪黑）
- Windows 游戏模式打开（只在它被关掉时改）
- 关闭 Xbox 后台录制（和本软件重复录制）
- 双显卡电脑把游戏设成用独显
- 打游戏时电源计划切到“高性能”，退出游戏后改回原计划
只提示、不自动改的：硬件加速 GPU 计划（要重启）、显卡驱动、内存 XMP、后台占用高的程序、笔记本电源。
不做：超频、改 BIOS、关闭杀毒和系统安全功能、改游戏文件、“优化包”一类的批量注册表修改。
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from .encoders import CREATE_NO_WINDOW

log = logging.getLogger(__name__)
IS_WINDOWS = sys.platform == "win32"

HIGH_PERF = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
ULTIMATE = "e9a42b02-d5df-448d-aa00-03f14749eb61"
BALANCED = "381b4222-f694-41f0-9685-ff5bb260df2e"
POWER_SAVER = "a1841308-3541-4fab-bc81-f71556f20b4a"
_GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

HKCU = "HKCU"
GAMEBAR = r"Software\Microsoft\GameBar"
GAMEDVR = r"Software\Microsoft\Windows\CurrentVersion\GameDVR"
GAMECONFIG = r"System\GameConfigStore"
GPU_PREF = r"Software\Microsoft\DirectX\UserGpuPreferences"
GAMEBAR_VALUES = [(GAMEDVR, "HistoricalCaptureEnabled"), (GAMEDVR, "AppCaptureEnabled"), (GAMECONFIG, "GameDVR_Enabled")]

DRIVER_LINKS = {"nvidia": "https://www.nvidia.cn/geforce/drivers/",
                "amd": "https://www.amd.com/zh-cn/support/download/drivers.html",
                "intel": "https://www.intel.cn/content/www/cn/zh/download-center/home.html"}
IGNORED_PROCS = {"_total", "idle", "system", "python", "pythonw", "qtwebengineprocess", "ffmpeg", "presentmon",
                 "dwm", "csrss", "audiodg", "memory compression", "registry", "secure system", "svchost"}

PERSISTENT_KEYS = ("refresh", "gamemode", "gamebar", "gpupref")
TITLES = {"refresh": "显示器刷新率", "power": "电源计划", "gamemode": "Windows 游戏模式", "gamebar": "Xbox 后台录制",
          "gpupref": "游戏使用独立显卡"}


def _decode(raw: bytes) -> str:
    for enc in (("oem", "mbcs") if IS_WINDOWS else ()) + ("utf-8",):
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


class WinSystem:
    """真实的 Windows 设置读写。每个方法都只碰一个具体的设置项。"""

    # ---- 注册表（只写当前用户 HKCU，不需要管理员权限） ----
    def reg_get(self, path: str, name: str) -> tuple[bool, object]:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ) as key:
                return True, winreg.QueryValueEx(key, name)[0]
        except OSError:
            return False, None

    def reg_set(self, path: str, name: str, value) -> None:
        import winreg
        kind = winreg.REG_DWORD if isinstance(value, int) else winreg.REG_SZ
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, kind, value)

    def reg_delete(self, path: str, name: str) -> None:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
        except OSError:
            pass

    # ---- 电源计划 ----
    def _powercfg(self, *args: str) -> tuple[int, str]:
        try:
            out = subprocess.run(["powercfg", *args], capture_output=True, timeout=20, creationflags=CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return -1, str(exc)
        return out.returncode, _decode(out.stdout + out.stderr)

    def power_active(self) -> tuple[str, str]:
        _code, text = self._powercfg("/getactivescheme")
        m = _GUID.search(text)
        name = text[text.rfind("(") + 1:text.rfind(")")] if "(" in text else ""
        return (m.group(0).lower() if m else ""), name.strip()

    def power_list(self) -> dict[str, str]:
        _code, text = self._powercfg("/list")
        plans = {}
        for line in text.splitlines():
            m = _GUID.search(line)
            if m:
                name = line[line.find("(") + 1:line.rfind(")")] if "(" in line else ""
                plans[m.group(0).lower()] = name.strip()
        return plans

    def power_set(self, guid: str) -> bool:
        return self._powercfg("/setactive", guid)[0] == 0

    def power_duplicate(self, guid: str) -> str:
        code, text = self._powercfg("-duplicatescheme", guid)
        m = _GUID.findall(text)
        return m[-1].lower() if code == 0 and m else ""

    def power_delete(self, guid: str) -> None:
        self._powercfg("-delete", guid)

    # ---- 显示器 ----
    def displays(self) -> list[dict]:
        from .hwinfo import display_modes
        return display_modes()

    def set_refresh(self, device: str, hz: int) -> tuple[bool, str]:
        from .hwinfo import change_refresh
        return change_refresh(device, hz)

    # ---- 进程占用（系统性能计数器，和语言无关） ----
    def top_processes(self) -> list[dict]:
        script = (
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8;$ErrorActionPreference='SilentlyContinue';"
            "$null=Get-CimInstance Win32_PerfFormattedData_PerfProc_Process;Start-Sleep -Milliseconds 1500;"
            "$n=(Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors;"
            "Get-CimInstance Win32_PerfFormattedData_PerfProc_Process | Sort-Object PercentProcessorTime -Descending |"
            "Select-Object -First 15 Name,PercentProcessorTime,WorkingSetPrivate,@{n='Cores';e={$n}} | ConvertTo-Json -Compress")
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                 capture_output=True, timeout=40, creationflags=CREATE_NO_WINDOW)
            data = json.loads(out.stdout.decode("utf-8", "replace").strip().lstrip("﻿") or "[]")
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return []
        rows = data if isinstance(data, list) else [data]
        result = []
        for r in rows:
            name = str(r.get("Name") or "").split("#")[0]
            if not name or name.lower() in IGNORED_PROCS:
                continue
            cores = max(1, int(r.get("Cores") or 1))
            result.append({"name": name, "cpu": float(r.get("PercentProcessorTime") or 0) / cores,
                           "mem_mb": int(r.get("WorkingSetPrivate") or 0) / 1024 ** 2})
        return result

    def file_exists(self, path: str) -> bool:
        return bool(path) and Path(path).exists()


class NullSystem:
    """非 Windows（开发机）上用：什么都不改，所有项显示“未检测”。"""

    def reg_get(self, path, name):
        return False, None

    def reg_set(self, path, name, value):
        raise OSError("只在 Windows 上可用")

    def reg_delete(self, path, name):
        pass

    def power_active(self):
        return "", ""

    def power_list(self):
        return {}

    def power_set(self, guid):
        return False

    def power_duplicate(self, guid):
        return ""

    def power_delete(self, guid):
        pass

    def displays(self):
        return []

    def set_refresh(self, device, hz):
        return False, "只在 Windows 上可用"

    def top_processes(self):
        return []

    def file_exists(self, path):
        return bool(path) and Path(path).exists()


def find_game_exe(exe_names: list[str]) -> str:
    """在“已安装的程序”注册表里找三角洲行动的安装目录，再在里面找游戏 exe。不打开游戏进程。"""
    if not IS_WINDOWS:
        return ""
    import winreg
    names = {n.lower() for n in exe_names if n}
    if not names:
        names = {"deltaforceclient-win64-shipping.exe"}
    locations = []
    roots = [(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")]
    for root, path in roots:
        try:
            with winreg.OpenKey(root, path) as key:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        with winreg.OpenKey(key, winreg.EnumKey(key, i)) as sub:
                            name = str(winreg.QueryValueEx(sub, "DisplayName")[0])
                            if "三角洲" in name or "delta force" in name.lower():
                                loc = str(winreg.QueryValueEx(sub, "InstallLocation")[0])
                                if loc:
                                    locations.append(loc)
                    except OSError:
                        continue
        except OSError:
            continue
    for loc in locations:
        base = Path(loc)
        for dirpath, dirnames, filenames in os.walk(base):
            if len(Path(dirpath).relative_to(base).parts) > 6:
                dirnames[:] = []
                continue
            for f in filenames:
                if f.lower() in names:
                    return str(Path(dirpath) / f)
    return ""


# ---------------------------------------------------------------------------
class Optimizer:
    def __init__(self, db, system=None):
        self.db = db
        self.sys = system or (WinSystem() if IS_WINDOWS else NullSystem())
        self.background: list[dict] = []
        self.background_at = 0.0

    # ---- 备份 ----
    def backup(self, key: str) -> dict | None:
        row = self.db.one("SELECT * FROM optimizer_backup WHERE key=?", (key,))
        if not row:
            return None
        return {"original": json.loads(row["original"] or "null"), "applied": json.loads(row["applied"] or "null"),
                "applied_at": row["applied_at"]}

    def _save_backup(self, key: str, original, applied) -> None:
        existing = self.backup(key)
        if existing:  # 已经备份过就保留最早的原值
            original = existing["original"]
        self.db.execute("INSERT INTO optimizer_backup(key,original,applied,applied_at) VALUES(?,?,?,?) "
                        "ON CONFLICT(key) DO UPDATE SET original=excluded.original, applied=excluded.applied, "
                        "applied_at=excluded.applied_at",
                        (key, json.dumps(original, ensure_ascii=False), json.dumps(applied, ensure_ascii=False), time.time()))

    def _drop_backup(self, key: str) -> None:
        self.db.execute("DELETE FROM optimizer_backup WHERE key=?", (key,))

    def changed_items(self) -> list[str]:
        return [TITLES.get(r["key"], r["key"]) for r in self.db.query("SELECT key FROM optimizer_backup ORDER BY applied_at")]

    # ---- 刷新率 ----
    def refresh_status(self) -> list[dict]:
        return [d for d in self.sys.displays() if d.get("hz")]

    def apply_refresh(self) -> tuple[bool, str]:
        changed, msgs, original = [], [], {}
        for d in self.refresh_status():
            if d["max_hz"] > d["hz"]:
                ok, msg = self.sys.set_refresh(d["device"], d["max_hz"])
                if ok:
                    original[d["device"]] = d["hz"]
                    changed.append(f"显示器 {d['index'] + 1}：{d['hz']}Hz → {d['max_hz']}Hz")
                else:
                    msgs.append(f"显示器 {d['index'] + 1} 改不了：{msg}")
        if original:
            prev = (self.backup("refresh") or {}).get("original") or {}
            self._save_backup("refresh", {**original, **prev}, {k: True for k in original})
        if changed:
            return True, "；".join(changed)
        return not msgs, "；".join(msgs) or "刷新率已经是最高"

    def restore_refresh(self) -> str:
        b = self.backup("refresh")
        if not b:
            return ""
        current = {d["device"]: d for d in self.refresh_status()}
        for device, hz in (b["original"] or {}).items():
            if device in current and current[device]["hz"] != hz:
                self.sys.set_refresh(device, int(hz))
        self._drop_backup("refresh")
        return "刷新率"

    # ---- 游戏模式 ----
    def gamemode_off(self) -> bool:
        exists, value = self.sys.reg_get(GAMEBAR, "AutoGameModeEnabled")
        return exists and value == 0  # 没有这个值时 Windows 默认是打开的

    def apply_gamemode(self) -> tuple[bool, str]:
        if not self.gamemode_off():
            return True, "游戏模式已经是打开的"
        exists, value = self.sys.reg_get(GAMEBAR, "AutoGameModeEnabled")
        self._save_backup("gamemode", {"exists": exists, "value": value}, {"value": 1})
        self.sys.reg_set(GAMEBAR, "AutoGameModeEnabled", 1)
        return True, "已打开 Windows 游戏模式"

    def restore_gamemode(self) -> str:
        b = self.backup("gamemode")
        if not b:
            return ""
        orig = b["original"] or {}
        if orig.get("exists"):
            self.sys.reg_set(GAMEBAR, "AutoGameModeEnabled", int(orig.get("value") or 0))
        else:
            self.sys.reg_delete(GAMEBAR, "AutoGameModeEnabled")
        self._drop_backup("gamemode")
        return "游戏模式"

    # ---- Xbox 后台录制 ----
    def gamebar_on(self) -> bool:
        return any(self.sys.reg_get(p, n) == (True, 1) for p, n in GAMEBAR_VALUES)

    def apply_gamebar(self) -> tuple[bool, str]:
        if not self.gamebar_on():
            return True, "Xbox 后台录制已经是关闭的"
        original = {}
        for path, name in GAMEBAR_VALUES:
            exists, value = self.sys.reg_get(path, name)
            original[f"{path}|{name}"] = {"exists": exists, "value": value}
        self._save_backup("gamebar", original, {"value": 0})
        for path, name in GAMEBAR_VALUES:
            self.sys.reg_set(path, name, 0)
        return True, "已关闭 Xbox 后台录制和游戏栏录制"

    def restore_gamebar(self) -> str:
        b = self.backup("gamebar")
        if not b:
            return ""
        for key, orig in (b["original"] or {}).items():
            path, _, name = key.partition("|")
            if orig.get("exists"):
                self.sys.reg_set(path, name, int(orig.get("value") or 0))
            else:
                self.sys.reg_delete(path, name)
        self._drop_backup("gamebar")
        return "Xbox 后台录制"

    # ---- 独显 ----
    def apply_gpupref(self, exe_path: str) -> tuple[bool, str]:
        if not exe_path or not self.sys.file_exists(exe_path):
            return False, "没有找到游戏程序的位置，请按说明手动设置"
        exists, value = self.sys.reg_get(GPU_PREF, exe_path)
        if exists and "GpuPreference=2" in str(value):
            return True, "游戏已经设为使用独立显卡"
        self._save_backup("gpupref", {"path": exe_path, "exists": exists, "value": value}, {"value": "GpuPreference=2;"})
        self.sys.reg_set(GPU_PREF, exe_path, "GpuPreference=2;")
        return True, "已把游戏设为使用独立显卡（下次启动游戏生效）"

    def restore_gpupref(self) -> str:
        b = self.backup("gpupref")
        if not b:
            return ""
        orig = b["original"] or {}
        if orig.get("exists"):
            self.sys.reg_set(GPU_PREF, orig["path"], str(orig.get("value") or ""))
        else:
            self.sys.reg_delete(GPU_PREF, orig.get("path", ""))
        self._drop_backup("gpupref")
        return "独显设置"

    # ---- 电源计划 ----
    def power_is_high(self) -> tuple[bool, str, str]:
        guid, name = self.sys.power_active()
        low = name.lower()
        high = guid in (HIGH_PERF, ULTIMATE) or "高性能" in name or "卓越" in name or "high performance" in low \
            or "ultimate" in low
        return high, guid, name

    def apply_power(self, session: bool) -> tuple[bool, str]:
        """session=True 表示只在这次游戏期间切换，游戏退出后改回。"""
        high, guid, name = self.power_is_high()
        if high:
            return True, f"电源计划已经是“{name or '高性能'}”"
        plans = self.sys.power_list()
        created = ""
        target = HIGH_PERF if HIGH_PERF in plans else next((g for g, n in plans.items() if "高性能" in n or
                                                           "high performance" in n.lower()), "")
        if not target:
            target = created = self.sys.power_duplicate(HIGH_PERF)
        if not target or not self.sys.power_set(target):
            if created:
                self.sys.power_delete(created)
            return False, "这台电脑不允许切换到高性能电源计划（部分笔记本只有“平衡”），已跳过"
        self._save_backup("power", {"guid": guid, "name": name, "created": created, "session": session},
                          {"guid": target})
        return True, f"电源计划：{name or '原计划'} → 高性能" + ("（退出游戏后自动改回）" if session else "")

    def restore_power(self, only_session: bool = False) -> str:
        b = self.backup("power")
        if not b:
            return ""
        orig = b["original"] or {}
        if only_session and not orig.get("session"):
            return ""
        if orig.get("guid"):
            self.sys.power_set(orig["guid"])
        if orig.get("created"):
            self.sys.power_delete(orig["created"])
        self._drop_backup("power")
        return "电源计划"

    # ---- 对外 ----
    def apply(self, key: str, exe_path: str = "") -> tuple[bool, str]:
        try:
            if key == "refresh":
                return self.apply_refresh()
            if key == "gamemode":
                return self.apply_gamemode()
            if key == "gamebar":
                return self.apply_gamebar()
            if key == "gpupref":
                return self.apply_gpupref(exe_path)
            if key == "power":
                return self.apply_power(session=False)
        except OSError as exc:
            log.warning("优化 %s 失败：%s", key, exc)
            return False, f"修改失败：{exc}"
        return False, "这一项只能手动处理"

    def restore(self, key: str | None = None) -> str:
        fns = {"refresh": self.restore_refresh, "gamemode": self.restore_gamemode, "gamebar": self.restore_gamebar,
               "gpupref": self.restore_gpupref, "power": self.restore_power}
        done = []
        for k, fn in fns.items():
            if key is None or key == k:
                try:
                    name = fn()
                except OSError as exc:
                    log.warning("还原 %s 失败：%s", k, exc)
                    continue
                if name:
                    done.append(name)
        if not done:
            return "没有需要还原的改动"
        return "已还原：" + "、".join(done)

    def auto_persistent(self, exe_path: str = "", hybrid: bool = False) -> list[str]:
        """开启自动优化后、游戏没运行时执行：刷新率、游戏模式、Xbox 录制、独显。返回这次真正改了的项。"""
        msgs = []
        for key in PERSISTENT_KEYS:
            if key == "gpupref" and not (hybrid and exe_path):
                continue
            before = self.backup(key)
            ok, msg = self.apply(key, exe_path)
            after = self.backup(key)
            if ok and after and (not before or after["applied_at"] != before["applied_at"]):
                msgs.append(msg)
        return msgs

    def sample_background(self) -> None:
        self.background = self.sys.top_processes()
        self.background_at = time.time()

    # ---- 页面数据 ----
    def items(self, hw: dict, auto: bool, exe_path: str, free_gb: float | None) -> list[dict]:
        from .hwinfo import gpu_names, ram_summary
        items = []

        # 刷新率
        displays = self.refresh_status()
        low = [d for d in displays if d["max_hz"] > d["hz"]]
        b = self.backup("refresh")
        if not displays:
            items.append(_item("refresh", "显示器刷新率", "desktop_windows", "unknown", "没有读到显示器信息。", ""))
        elif low:
            d = low[0]
            items.append(_item("refresh", "显示器刷新率", "desktop_windows", "warn",
                               f"显示器 {d['index'] + 1} 当前 {d['hz']}Hz，最高支持 {d['max_hz']}Hz。高刷屏跑在低刷新率，画面更卡、更难跟枪。",
                               f"{d['width']}×{d['height']} · {d['hz']}Hz", can_apply=True, apply_label="调到最高",
                               check=(f"显示器 {d['max_hz']}Hz 刷新率", "未在最高")))
        else:
            d = displays[0]
            items.append(_item("refresh", "显示器刷新率", "desktop_windows", "applied" if b else "ok",
                               ("已调到最高 " if b else "已在最高 ") + f"{d['hz']}Hz。", f"{d['width']}×{d['height']} · {d['hz']}Hz",
                               applied=bool(b), check=(f"显示器 {d['hz']}Hz 刷新率", "已改回最高" if b else "已在最高")))

        # 电源计划
        high, _guid, name = self.power_is_high()
        b = self.backup("power")
        if high:
            items.append(_item("power", "系统电源计划", "bolt", "applied" if b else "ok",
                               f"当前是“{name}”。" + ("退出游戏后会改回原计划。" if b and (b["original"] or {}).get("session") else ""),
                               f"原计划：{(b['original'] or {}).get('name', '')}" if b else (name or ""),
                               applied=bool(b), check=("电源计划：高性能", "已切换" if b else "已是高性能")))
        else:
            saver = "节能" in name or "saver" in name.lower()
            detail = (f"当前是“{name or '未知'}”。" + ("开启了自动优化：打游戏时切到高性能，退出游戏后改回。" if auto else
                      "改为高性能通常能提高 1% Low 帧率。"))
            items.append(_item("power", "系统电源计划", "bolt", "warn" if saver else ("ok" if auto else "info"), detail,
                               f"当前：{name or '未知'}", can_apply=not auto, apply_label="切到高性能",
                               check=("电源计划", "打游戏时切高性能" if auto else (name or "未知"))))

        # 游戏模式
        b = self.backup("gamemode")
        if self.gamemode_off():
            items.append(_item("gamemode", "Windows 游戏模式", "sports_esports", "warn", "游戏模式被关掉了。打开后 Windows 会优先把资源给游戏。",
                               "AutoGameModeEnabled = 0", can_apply=True, apply_label="打开", check=("Windows 游戏模式", "已关闭")))
        else:
            items.append(_item("gamemode", "Windows 游戏模式", "sports_esports", "applied" if b else "ok", "已开启。",
                               "AutoGameModeEnabled = 1" if b else "系统默认开启", applied=bool(b),
                               check=("Windows 游戏模式", "已开启")))

        # Xbox 后台录制
        b = self.backup("gamebar")
        if self.gamebar_on():
            items.append(_item("gamebar", "Xbox 后台录制", "videocam_off", "warn",
                               "Xbox 游戏栏的录制开着，会和本软件重复录制、多占一份显卡编码和硬盘。", "HistoricalCaptureEnabled = 1",
                               can_apply=True, apply_label="关闭", check=("Xbox 后台录制", "开着")))
        else:
            items.append(_item("gamebar", "Xbox 后台录制", "videocam_off", "applied" if b else "ok",
                               "已关闭，避免和本软件重复录制。", "HistoricalCaptureEnabled = 0", applied=bool(b),
                               check=("Xbox 后台录制", "已关闭")))

        # 双显卡
        names = [n.lower() for n in gpu_names(hw)]
        hybrid = is_hybrid(names)
        if hybrid:
            b = self.backup("gpupref")
            if b:
                items.append(_item("gpupref", "游戏使用独立显卡", "developer_board", "applied", "已把游戏设为使用独立显卡。",
                                   Path(exe_path).name if exe_path else "", applied=True, check=("游戏使用独显", "已设置")))
            elif exe_path:
                items.append(_item("gpupref", "游戏使用独立显卡", "developer_board", "info",
                                   "检测到独显和核显。把游戏设为“高性能（独显）”，避免跑在核显上。", Path(exe_path).name,
                                   can_apply=True, apply_label="设为独显", check=("游戏使用独显", "未设置")))
            else:
                items.append(_item("gpupref", "游戏使用独立显卡", "developer_board", "info",
                                   "检测到独显和核显，但没找到游戏程序位置。可以手动设置：Windows 设置 → 系统 → 屏幕 → 显示卡 → "
                                   "添加游戏的 exe → 选“高性能”。", "需要手动设置", check=("游戏使用独显", "需手动确认")))

        # 显卡驱动
        gpus = hw.get("gpu") or []
        main = next((g for g in gpus if any(k in str(g.get("Name", "")).lower() for k in ("nvidia", "geforce", "radeon", "arc"))),
                    gpus[0] if gpus else None)
        if main:
            gname = str(main.get("Name", ""))
            vendor = "nvidia" if ("nvidia" in gname.lower() or "geforce" in gname.lower()) else \
                "amd" if ("amd" in gname.lower() or "radeon" in gname.lower()) else "intel"
            age = _days_since(str(main.get("DriverDate") or ""))
            stale = age is not None and age > 180
            items.append(_item("driver", "显卡驱动版本", "memory", "info" if stale else "ok",
                               f"{gname} 驱动 {main.get('DriverVersion', '')}" +
                               (f"，发布于约 {age // 30} 个月前，建议到官网更新。" if stale else "，比较新。"),
                               str(main.get("DriverDate") or ""), link=DRIVER_LINKS[vendor] if stale else "",
                               link_label="官方下载", check=("显卡驱动", "建议更新" if stale else "较新")))

        # 内存
        ram = ram_summary(hw)
        if ram:
            warn = ram["total_gb"] < 15.5
            slow = (ram["kind"] == "DDR4" and 0 < ram["speed"] < 3000) or (ram["kind"] == "DDR5" and 0 < ram["speed"] < 4800)
            single = ram["sticks"] == 1
            if warn:
                detail, status = f"内存 {ram['total_gb']:.0f} GB，低于 16 GB，大地图容易卡顿。", "warn"
            elif slow:
                detail, status = f"运行在 {ram['speed']} MT/s，可能没开 XMP/EXPO（要在主板 BIOS 里打开，本软件不改 BIOS）。", "info"
            elif single:
                detail, status = "只有 1 根内存，是单通道，换成 2 根组双通道能明显提升帧率。", "info"
            else:
                detail, status = f"运行在 {ram['speed']} MT/s，{'双通道' if ram['sticks'] >= 2 else ''}。", "ok"
            items.append(_item("xmp", "内存 XMP / EXPO", "developer_board", status, detail,
                               f"{ram['total_gb']:.0f} GB {ram['kind']} · {ram['sticks']} 根", check=(f"内存 {ram['kind']} {ram['speed']} MT/s", "需检查" if status != "ok" else "正常")))

        # 后台进程
        heavy = [p for p in self.background if p["cpu"] >= 8]
        if self.background_at:
            if heavy:
                top = heavy[0]
                items.append(_item("background", "后台高占用进程", "hourglass_top", "info",
                                   f"{top['name']} 占用 CPU 约 {top['cpu']:.0f}%，进对局前可以关掉不用的程序。",
                                   "只提示，不会强制关闭", check=("后台占用", top["name"])))
            else:
                items.append(_item("background", "后台高占用进程", "hourglass_top", "ok", "没有占用明显的后台程序。",
                                   "只提示，不会强制关闭", check=("后台占用", "正常")))

        # 硬件加速 GPU 计划
        hags = (hw.get("registry") or {}).get("hags")
        if hw.get("registry"):
            items.append(_item("hags", "硬件加速 GPU 计划", "speed", "ok" if hags == 2 else "info",
                               "已开启。" if hags == 2 else "未开启。可在 Windows 设置 → 系统 → 屏幕 → 显示卡 → 更改默认图形设置 里打开，"
                               "需要重启；部分显卡上能降低延迟，效果因电脑而异。", "需要重启才生效" if hags != 2 else "HwSchMode = 2",
                               check=("硬件加速 GPU 计划", "已开启" if hags == 2 else "未开启")))

        # 笔记本电源
        for bat in hw.get("battery") or []:
            if bat.get("BatteryStatus") in (1, 4, 5):
                items.append(_item("battery", "笔记本电源", "bolt", "warn", "笔记本没插电源，显卡会降频，请插上电源再玩。",
                                   f"电量 {bat.get('EstimatedChargeRemaining', '?')}%", check=("笔记本电源", "未插电")))
                break

        # 录像硬盘空间
        if free_gb is not None:
            items.append(_item("storage", "录像硬盘空间", "hard_drive", "warn" if free_gb < 50 else "ok",
                               f"录像所在硬盘剩余 {free_gb:.0f} GB" + ("，1 小时录像约 9 GB，建议清理。" if free_gb < 50 else "。"),
                               "低于 10 GB 时自动暂停录制", check=("录像硬盘空间", f"剩 {free_gb:.0f} GB")))
        return items


def is_hybrid(names: list[str]) -> bool:
    dgpu = any(("nvidia" in n or "geforce" in n or "radeon rx" in n or "arc a" in n) for n in names)
    igpu = any(("intel" in n and "arc a" not in n) or "radeon(tm) graphics" in n or n.endswith("radeon graphics")
               for n in names)
    return dgpu and igpu


def _item(key, title, icon, status, detail, footer, *, applied=False, can_apply=False, apply_label="", link="",
          link_label="", check=None) -> dict:
    return {"key": key, "title": title, "icon": icon, "status": status, "detail": detail, "footer": footer,
            "applied": applied, "can_apply": can_apply, "apply_label": apply_label, "link": link,
            "link_label": link_label, "footer_right": "", "check": {"label": check[0], "value": check[1]} if check else None}


def _days_since(date: str) -> int | None:
    try:
        return int((time.time() - time.mktime(time.strptime(date[:10], "%Y-%m-%d"))) // 86400)
    except (ValueError, OverflowError):
        return None
