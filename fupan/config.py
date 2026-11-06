"""用户设置。保存在 config.json；API Key 不在这里，单独存在系统凭据管理器里。"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

CONFIG_FILE = "config.json"


@dataclass
class Config:
    auto_record: bool = True
    recordings_dir: str = ""
    retention_days: int = 3
    min_free_gb: float = 10.0
    framerate: int = 60
    bitrate_mbps: int = 0  # 0 = 按分辨率自动
    segment_minutes: int = 5  # 旧版本用的字段，保留兼容
    segment_seconds: int = 60  # 录像分段长度：录完一段就可以分析
    monitor_index: int = -1  # -1 = 自动
    encoder: str = "auto"  # auto 或某个编码器名
    encoder_cache: dict = field(default_factory=dict)  # {"gpu": 显卡名, "encoder": 名称}
    foreground_start_sec: float = 2.0
    foreground_stop_sec: float = 3.0
    game_title_keywords: list = field(default_factory=lambda: ["三角洲行动", "Delta Force"])
    game_process_names: list = field(default_factory=list)  # 用户确认过的游戏进程名，优先使用
    min_window_width: int = 1024
    min_window_height: int = 576
    ai_provider: str = "deepseek"  # deepseek / qwen / doubao / zhipu（见 aiclient.PROVIDERS）
    ai_base_url: str = ""  # 空 = 用这个平台的默认地址
    ai_text_model: str = ""
    ai_vision_model: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"  # 0.3.x 的旧字段，读取时迁移到 ai_*
    deepseek_text_model: str = ""
    deepseek_vision_model: str = ""
    ai_vision_enabled: bool = False  # 开启后才会把截图发给 AI
    ai_daily_token_limit: int = 1000000  # 每局都分析，一局约 5～7 万 tokens
    first_run_done: bool = False
    auto_optimize: bool = False  # 赛前自动优化（首次使用时询问）
    restore_power_after_game: bool = True
    post_auto_analyze: bool = True  # 游戏关闭后自动整理录像（缩略图；开了 AI 还会找阵亡）
    live_analyze: bool = True  # 边录边分析：每局结束马上出结果（后台最低优先级）
    thumb_step: int = 10
    profile: dict = field(default_factory=dict)  # 用户填写的游戏灵敏度设置
    gunsmith_budget: str = ""
    gunsmith_auto_refresh_hours: int = 24
    next_measure_label: str = "before"

    def recordings_path(self) -> Path:
        return Path(self.recordings_dir) if self.recordings_dir else paths.default_recordings_dir()


def config_path() -> Path:
    return paths.config_dir() / CONFIG_FILE


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    cfg = Config()
    if not path.exists():
        return cfg
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("配置文件读取失败，使用默认设置：%s", exc)
        return cfg
    defaults = asdict(Config())
    for key, value in raw.items():
        if key not in defaults:
            continue
        default = defaults[key]
        # 类型不对的值（手改坏了的配置文件）忽略，用默认值
        if isinstance(default, bool) and not isinstance(value, bool):
            continue
        if isinstance(default, (int, float)) and not isinstance(default, bool) and \
                (isinstance(value, bool) or not isinstance(value, (int, float))):
            continue
        if isinstance(default, (str, list, dict)) and not isinstance(value, type(default)):
            continue
        setattr(cfg, key, value)
    # 0.4.0 起 AI 设置不再只认 DeepSeek：旧字段搬到通用字段
    if not raw.get("ai_vision_model") and not raw.get("ai_text_model"):
        cfg.ai_text_model = cfg.ai_text_model or cfg.deepseek_text_model
        cfg.ai_vision_model = cfg.ai_vision_model or cfg.deepseek_vision_model
        if cfg.deepseek_base_url and cfg.deepseek_base_url.rstrip("/") != "https://api.deepseek.com":
            cfg.ai_base_url = cfg.deepseek_base_url
    # 0.3.0 起每局都分析：旧版本默认的 50 万上限（用户没改过的）调到 100 万
    if raw.get("ai_daily_token_limit") == 500000 and not raw.get("limits_v3"):
        cfg.ai_daily_token_limit = 1000000
    return cfg


def save(cfg: Config, path: Path | None = None) -> None:
    path = path or config_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({**asdict(cfg), "limits_v3": True}, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
