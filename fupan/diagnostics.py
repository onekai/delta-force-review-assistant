"""诊断包：把排查问题需要的信息打成一个 zip 放到桌面，方便发给开发者。

包含：设置（不含密钥）、日志、配置检测结果、编码器自检记录、最近 3 个场次的 session.json 和 FFmpeg 日志。
不包含：录像视频、API Key、其他窗口的标题。
"""

from __future__ import annotations

import json
import time
import zipfile
from dataclasses import asdict
from pathlib import Path

from . import __version__, paths
from .config import Config
from .logutil import redact
from .sessions import list_sessions


def export(cfg: Config, hw: dict, encoder_report: list[dict], game_exes: list[str], extra: dict | None = None) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    target = paths.desktop_dir() / f"三角洲复盘助手诊断_{stamp}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("version.txt", __version__)
        if extra:
            zf.writestr("backend.json", redact(json.dumps(extra, ensure_ascii=False, indent=2, default=str)))
        zf.writestr("config.json", json.dumps(asdict(cfg), ensure_ascii=False, indent=2))
        zf.writestr("hwinfo.json", json.dumps(hw, ensure_ascii=False, indent=2, default=str))
        zf.writestr("encoder_report.json", json.dumps(encoder_report, ensure_ascii=False, indent=2))
        zf.writestr("large_window_exes.json", json.dumps(sorted(set(game_exes)), ensure_ascii=False, indent=2))
        for log_file in sorted(paths.log_dir().glob("*.log*")):
            zf.writestr(f"logs/{log_file.name}", redact(log_file.read_text(encoding="utf-8", errors="replace")))
        for summary in list_sessions(cfg.recordings_path())[:3]:
            for item in summary.folder.iterdir():
                if item.name == "session.json" or item.name.endswith("_ffmpeg.log"):
                    zf.write(item, f"sessions/{summary.name}/{item.name}")
    return target
