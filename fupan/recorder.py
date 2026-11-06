"""录制控制。

规则：游戏窗口在前台满 2 秒、而且窗口大小稳定 1 秒后开始录；切到后台满 3 秒暂停；游戏关闭就结束这个场次。
每段录像按 60 秒切分成 MKV 文件：程序或电脑崩溃时最多丢最后一分钟，录完的分段马上可以拿去分析。

游戏启动、切换全屏、改分辨率时，系统截屏接口（DDA）会中断，FFmpeg 报“Output changed”后退出。
这种情况 1 秒后自动续录，不算失败；真正的错误按 2、4、8……最长 60 秒的间隔重试。
游戏开着就一直重试，不会整场放弃。状态机不依赖界面，由界面的定时器每秒调用一次 tick()。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from dataclasses import asdict
from pathlib import Path

from . import __version__, encoders, winapi
from .config import Config
from .game_watch import GameState
from .sessions import Run, Session, new_session_name, write_session
from .winapi import MonitorInfo, Rect

log = logging.getLogger(__name__)

OUTPUT_CHANGED_MARKS = ("output changed", "output parameters changed", "access lost", "access_lost")
STABLE_SEC = 1.0
CHANGE_RETRY_SEC = 1.0
MAX_RETRY_SEC = 60.0


def compute_crop(win: Rect, mon: Rect) -> encoders.Crop:
    left, top = max(win.left, mon.left), max(win.top, mon.top)
    right, bottom = min(win.right, mon.right), min(win.bottom, mon.bottom)
    return encoders.Crop(left - mon.left, top - mon.top, encoders.even(right - left), encoders.even(bottom - top))


def free_gb(folder: Path) -> float:
    probe_dir = folder
    while not probe_dir.exists() and probe_dir != probe_dir.parent:
        probe_dir = probe_dir.parent
    try:
        return shutil.disk_usage(probe_dir).free / 1024 ** 3
    except OSError:
        return 0.0


class FFmpegProcess:
    def __init__(self, cmd: list[str], log_file: Path):
        self.cmd = cmd
        self.log_file = log_file
        self.stats: dict[str, str] = {}
        self._log_handle = open(log_file, "w", encoding="utf-8", errors="replace")
        self._log_handle.write(" ".join(cmd) + "\n\n")
        self._log_handle.flush()
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self._log_handle, creationflags=encoders.CREATE_NO_WINDOW)
        self._reader = threading.Thread(target=self._read_progress, daemon=True)
        self._reader.start()

    def _read_progress(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if "=" in line:
                key, _, value = line.partition("=")
                self.stats[key] = value

    def alive(self) -> bool:
        return self.proc.poll() is None

    def exit_reason(self) -> str:
        """FFmpeg 退出后看日志最后几行：截屏画面参数变了返回 output_changed，其他返回 error。"""
        try:
            with open(self.log_file, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 4096))
                tail = f.read().decode("utf-8", "replace").lower()
        except OSError:
            return "error"
        return "output_changed" if any(m in tail for m in OUTPUT_CHANGED_MARKS) else "error"

    def stop(self, timeout: float = 8.0) -> int | None:
        if self.alive():
            try:
                assert self.proc.stdin is not None
                self.proc.stdin.write(b"q")
                self.proc.stdin.flush()
                self.proc.wait(timeout=timeout)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        self._log_handle.close()
        return self.proc.returncode


class RecordingController:
    def __init__(self, cfg: Config, ffmpeg: str = ""):
        self.cfg = cfg
        self.ffmpeg = ffmpeg or encoders.find_ffmpeg()
        self.encoder = ""  # 由后台在自检后填入
        self.session: Session | None = None
        self.session_dir: Path | None = None
        self.run: Run | None = None
        self.proc: FFmpegProcess | None = None
        self.status = "等待游戏启动"
        self.error = ""
        self.output_changes = 0  # 本场因为游戏切换画面而自动续录的次数
        self.failures = 0  # 本场真正出错的次数
        self._fg_since: float | None = None
        self._bg_since: float | None = None
        self._last_rect: Rect | None = None
        self._stable_since: float | None = None
        self._retry_at = 0.0
        self._fail_streak = 0
        self._last_reason = ""
        self._last_crop: encoders.Crop | None = None
        self.monitor_provider = winapi.list_monitors

    # ---- 对外状态 ----
    @property
    def recording(self) -> bool:
        return self.proc is not None and self.proc.alive()

    def live_stats(self) -> dict:
        if not self.proc:
            return {}
        s = self.proc.stats
        return {"fps": s.get("fps", ""), "frame": s.get("frame", ""), "drop": s.get("drop_frames", "0"),
                "dup": s.get("dup_frames", "0"), "time": s.get("out_time", "")}

    def current_files(self) -> tuple[Path | None, str]:
        """(当前场次文件夹, 正在写的这段录像的文件名前缀)。正在写的分段不能拿去分析。"""
        if self.session_dir is None:
            return None, ""
        return self.session_dir, (self.run.pattern.split("%")[0] if self.run and self.recording else "")

    # ---- 状态机 ----
    def tick(self, state: GameState, now: float | None = None) -> None:
        now = now or time.time()
        if not self.cfg.auto_record:
            self._stop_run(now, "off")
            if not state.running:
                self._close_session(now)
            self.status = "自动录制已关闭"
            return

        if not state.running:
            self._stop_run(now, "exit")
            self._close_session(now)
            self._fg_since = self._bg_since = None
            self.status = "等待游戏启动"
            return

        if self.session is None:
            self._open_session(now, state)

        if state.foreground:
            self._bg_since = None
            if self._fg_since is None:
                self._fg_since = now
            rect = state.window.rect if state.window else None
            if rect != self._last_rect:
                self._last_rect = rect
                self._stable_since = now
            if self.proc is None:
                if now < self._retry_at:
                    self.status = ("游戏画面切换中，马上继续录制" if self._last_reason == "output_changed"
                                   else f"录制出错，{int(self._retry_at - now) + 1} 秒后自动重试")
                elif now - self._fg_since < self.cfg.foreground_start_sec or now - (self._stable_since or now) < STABLE_SEC:
                    self.status = "游戏在前台，即将开始录制"
                else:
                    self._start_run(now, state)
                return
            if not self.proc.alive():
                self._handle_exit(now)
                return
            crop = self._crop_for(state)
            if crop is not None and crop != self._last_crop:
                log.info("游戏窗口大小或位置变了，重新开始一段录制")
                self._stop_run(now, "resize")
                self._start_run(now, state)
            return

        self._fg_since = None
        if self.proc is not None:
            if not self.proc.alive():
                self._handle_exit(now)
                return
            if self._bg_since is None:
                self._bg_since = now
            if now - self._bg_since >= self.cfg.foreground_stop_sec:
                self._stop_run(now, "background")
                self.status = "游戏不在前台，已暂停录制"
        else:
            self.status = "游戏不在前台，暂停录制"

    def shutdown(self) -> None:
        now = time.time()
        self._stop_run(now, "quit")
        self._close_session(now)

    # ---- 内部 ----
    def _open_session(self, now: float, state: GameState) -> None:
        name = new_session_name(now)
        self.session_dir = self.cfg.recordings_path() / name
        self.session = Session(name=name, started_at=now, game_title=state.window.title if state.window else "",
                               game_exe=state.window.exe if state.window else "", app_version=__version__)
        self.error = ""
        self.output_changes = self.failures = 0
        self._fail_streak = 0
        self._retry_at = 0.0
        self._last_reason = ""
        write_session(self.session_dir, self.session)
        log.info("检测到游戏，新建场次 %s（%s）", name, self.session.game_exe or self.session.game_title)

    def _close_session(self, now: float) -> None:
        if self.session is None or self.session_dir is None:
            return
        self.session.ended_at = now
        write_session(self.session_dir, self.session)
        log.info("场次结束 %s（自动续录 %d 次，出错 %d 次）", self.session.name, self.output_changes, self.failures)
        self.session = None
        self.session_dir = None

    def _crop_for(self, state: GameState) -> encoders.Crop | None:
        if state.window is None:
            return None
        mon = self._monitor_for(state)
        if mon is None:
            return None
        return compute_crop(state.window.rect, mon.rect)

    def _monitor_for(self, state: GameState) -> MonitorInfo | None:
        monitors = self.monitor_provider()
        if not monitors:
            return None
        if 0 <= self.cfg.monitor_index < len(monitors):
            return monitors[self.cfg.monitor_index]
        return winapi.monitor_for_rect(state.window.rect, monitors) if state.window else monitors[0]

    def _start_run(self, now: float, state: GameState) -> None:
        if not self.ffmpeg:
            self.status = "缺少 FFmpeg，无法录制（请重新运行安装脚本）"
            return
        if not self.encoder:
            self.status = "正在检测显卡编码器，稍后自动开始录制"
            return
        assert self.session is not None and self.session_dir is not None
        if free_gb(self.session_dir) < self.cfg.min_free_gb:
            self.status = f"硬盘剩余空间不足 {self.cfg.min_free_gb:.0f} GB，暂停录制"
            return
        mon = self._monitor_for(state)
        if mon is None or state.window is None:
            self.status = "没有找到显示器信息，无法录制"
            return
        crop = compute_crop(state.window.rect, mon.rect)
        bitrate = self.cfg.bitrate_mbps or encoders.auto_bitrate_mbps(crop.height)
        index = len(self.session.runs) + 1
        pattern = self.session_dir / f"run{index:02d}_%03d.mkv"
        cmd = encoders.record_command(self.ffmpeg, self.encoder, mon.index, crop, self.cfg.framerate,
                                      bitrate, max(10, int(self.cfg.segment_seconds)), pattern)
        try:
            self.proc = FFmpegProcess(cmd, self.session_dir / f"run{index:02d}_ffmpeg.log")
        except OSError as exc:
            self.status = f"启动录制失败：{exc}"
            log.exception("启动 FFmpeg 失败")
            self._schedule_retry(now, "error", ran=0)
            return
        self.run = Run(index=index, started_at=now, encoder=self.encoder, monitor_index=mon.index,
                       crop=asdict(crop), pattern=pattern.name)
        self.session.runs.append(self.run)
        write_session(self.session_dir, self.session)
        self._last_crop = crop
        self.status = f"录制中（{crop.width}×{crop.height}，{self.encoder}）"
        log.info("开始录制第 %d 段：显示器 %d，裁剪 %s，%d Mbps", index, mon.index, crop, bitrate)

    def _stop_run(self, now: float, reason: str) -> None:
        if self.proc is None:
            return
        code = self.proc.stop()
        if self.run is not None:
            self.run.ended_at = now
            self.run.exit_code = code
            self.run.reason = reason
        if self.session is not None and self.session_dir is not None:
            write_session(self.session_dir, self.session)
        log.info("停止录制（%s），FFmpeg 退出码 %s", reason, code)
        self.proc = None
        self.run = None
        self._last_crop = None

    def _handle_exit(self, now: float) -> None:
        assert self.proc is not None
        reason = self.proc.exit_reason()
        code = self.proc.proc.returncode
        ran = now - (self.run.started_at if self.run else now)
        self._stop_run(now, reason)
        self._schedule_retry(now, reason, ran, code)

    def _schedule_retry(self, now: float, reason: str, ran: float, code=None) -> None:
        self._last_reason = reason
        if reason == "output_changed":
            self.output_changes += 1
            self._retry_at = now + CHANGE_RETRY_SEC
            self.status = "游戏画面切换中，马上继续录制"
            log.info("截屏画面参数变了（游戏切换全屏或分辨率），%.0f 秒后续录", CHANGE_RETRY_SEC)
            return
        self.failures += 1
        self._fail_streak = self._fail_streak + 1 if ran < 60 else 1
        delay = min(MAX_RETRY_SEC, 2.0 ** self._fail_streak)
        self._retry_at = now + delay
        self.error = f"FFmpeg 退出码 {code}" if code is not None else "启动录制失败"
        self.status = f"录制出错，{int(delay)} 秒后自动重试"
        log.warning("录制意外停止（退出码 %s），%.0f 秒后重试", code, delay)
