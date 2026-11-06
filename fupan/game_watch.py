"""找到游戏窗口，判断它是否在前台。匹配规则独立成纯函数，方便测试。"""

from __future__ import annotations

from dataclasses import dataclass

from . import winapi
from .config import Config
from .winapi import WindowInfo


@dataclass(frozen=True)
class GameState:
    window: WindowInfo | None
    foreground: bool
    minimized: bool = False  # 游戏窗口被最小化了（切到桌面时独占全屏游戏会这样），游戏仍算开着

    @property
    def running(self) -> bool:
        return self.window is not None or self.minimized


def matches_game(win: WindowInfo, cfg: Config) -> bool:
    names = {n.lower() for n in cfg.game_process_names if n}
    if names:
        return win.exe.lower() in names
    title = win.title.lower()
    return any(k.lower() in title for k in cfg.game_title_keywords if k)


def is_candidate(win: WindowInfo, cfg: Config) -> bool:
    if not win.visible or win.minimized:
        return False
    if win.rect.width < cfg.min_window_width or win.rect.height < cfg.min_window_height:
        return False
    return matches_game(win, cfg)


def find_game_window(windows: list[WindowInfo], cfg: Config) -> WindowInfo | None:
    candidates = [w for w in windows if is_candidate(w, cfg)]
    if not candidates:
        return None
    return max(candidates, key=lambda w: w.rect.width * w.rect.height)


class GameWatcher:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def poll(self) -> GameState:
        windows = winapi.list_windows()
        win = find_game_window(windows, self.cfg)
        if win is None:
            minimized = any(w.visible and w.minimized and matches_game(w, self.cfg) for w in windows)
            return GameState(None, False, minimized)
        return GameState(win, winapi.foreground_hwnd() == win.hwnd)


def describe_windows_for_picker() -> list[WindowInfo]:
    """设置页“选择游戏窗口”用：列出所有可见、尺寸够大的窗口。"""
    return [w for w in winapi.list_windows() if w.visible and w.rect.width >= 640 and w.rect.height >= 360]
