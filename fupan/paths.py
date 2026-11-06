"""程序用到的目录。Windows 上放在用户目录下，其他系统（开发测试用）放在家目录下。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import APP_ID

IS_WINDOWS = sys.platform == "win32"


def _env_dir(name: str, fallback: Path) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else fallback


def _override() -> Path | None:
    """设置了 FUPAN_HOME 时，设置和数据都放在这个目录下（测试和便携使用）。"""
    value = os.environ.get("FUPAN_HOME")
    return Path(value) if value else None


def config_dir() -> Path:
    if _override():
        path = _override() / "config"
    else:
        base = _env_dir("APPDATA", Path.home() / ".config") if IS_WINDOWS else Path.home() / ".config"
        path = base / APP_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


def data_dir() -> Path:
    if _override():
        path = _override() / "data"
    else:
        base = _env_dir("LOCALAPPDATA", Path.home() / ".local" / "share") if IS_WINDOWS else Path.home() / ".local" / "share"
        path = base / APP_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_dir() -> Path:
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_recordings_dir() -> Path:
    if _override():
        return _override() / "recordings"
    return Path.home() / "Videos" / APP_ID


def app_root() -> Path:
    """程序所在目录：打包后是 exe 所在目录，源码运行时是项目根目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def desktop_dir() -> Path:
    if _override():
        path = _override() / "desktop"
        path.mkdir(parents=True, exist_ok=True)
        return path
    desktop = Path.home() / "Desktop"
    return desktop if desktop.exists() else Path.home()
