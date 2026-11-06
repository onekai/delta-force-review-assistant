"""录像场次：每次游戏启动到关闭是一个场次文件夹，里面是分段录像和 session.json。"""

from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

SESSION_FILE = "session.json"


@dataclass
class Run:
    """游戏在前台时的一段连续录制；切到后台或改分辨率会开始新的一段。"""
    index: int
    started_at: float
    ended_at: float = 0.0
    encoder: str = ""
    monitor_index: int = 0
    crop: dict = field(default_factory=dict)
    pattern: str = ""
    exit_code: int | None = None
    reason: str = ""  # 这段为什么停：background / resize / output_changed / error / exit / quit / off


@dataclass
class Session:
    name: str
    started_at: float
    ended_at: float = 0.0
    game_title: str = ""
    game_exe: str = ""
    app_version: str = ""
    keep: bool = False
    runs: list = field(default_factory=list)


def new_session_name(now: float | None = None) -> str:
    return time.strftime("%Y%m%d_%H%M%S", time.localtime(now or time.time()))


def write_session(folder: Path, session: Session) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    data = asdict(session)
    data["runs"] = [asdict(r) if isinstance(r, Run) else r for r in session.runs]
    tmp = folder / (SESSION_FILE + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(folder / SESSION_FILE)


def read_session(folder: Path) -> dict | None:
    try:
        return json.loads((folder / SESSION_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


@dataclass(frozen=True)
class SessionSummary:
    folder: Path
    name: str
    started_at: float
    duration_sec: float
    size_bytes: int
    video_files: int
    keep: bool


def folder_size(folder: Path) -> tuple[int, int]:
    total, videos = 0, 0
    for item in folder.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
            if item.suffix.lower() in (".mkv", ".mp4"):
                videos += 1
    return total, videos


EXPORT_DIR = "单局录像"  # 录像文件夹里保存整局录像的子文件夹：不算场次，也不会被自动清理


def list_sessions(root: Path) -> list[SessionSummary]:
    if not root.exists():
        return []
    result = []
    for folder in root.iterdir():
        if not folder.is_dir() or folder.name == EXPORT_DIR:
            continue
        data = read_session(folder) or {}
        started = float(data.get("started_at") or folder.stat().st_mtime)
        duration = 0.0
        for run in data.get("runs", []):
            if run.get("ended_at"):
                duration += max(0.0, run["ended_at"] - run["started_at"])
        size, videos = folder_size(folder)
        result.append(SessionSummary(folder, folder.name, started, duration, size, videos, bool(data.get("keep"))))
    result.sort(key=lambda s: s.started_at, reverse=True)
    return result


def cleanup(root: Path, retention_days: int, now: float | None = None, active: Path | None = None) -> list[Path]:
    """删除超过保留天数、且没有标记保留的场次。正在录制的场次不会删。"""
    if retention_days <= 0:
        return []
    now = now or time.time()
    cutoff = now - retention_days * 86400
    removed = []
    for summary in list_sessions(root):
        if summary.keep or (active is not None and summary.folder == active):
            continue
        if summary.started_at < cutoff:
            shutil.rmtree(summary.folder, ignore_errors=True)
            removed.append(summary.folder)
            log.info("已按保留天数删除旧录像：%s", summary.name)
    return removed
