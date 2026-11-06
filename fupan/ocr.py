"""本机文字识别：用 Windows 自带的 OCR（不联网、不花钱）认出画面里的固定文字，判断“现在在大厅、结算还是对局中”。

按局切分最需要的就是“大厅 / 不是大厅”这一条：三角洲行动大厅顶部一直有“开始游戏 · 仓库 · 特战干员 · 部门 · 交易行 · 特勤处”菜单，
同一张画面里认出两个以上，就是在大厅。结算画面有“撤离成功 / 撤离失败”。阵亡、倒地的提示文字是根据常见写法猜的，
认不到时由 AI 补（开了 AI 看图的话）。

只在 Windows 10/11 上可用（需要系统装有中文 OCR 语言包，中文版 Windows 默认就有）；用不了时自动跳过。
所有识别都在单独的一个线程里做（WinRT 需要先初始化这个线程）。
"""

from __future__ import annotations

import asyncio
import ctypes
import importlib
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor

from .matches import MAPS

log = logging.getLogger(__name__)

LOBBY_WORDS = ("开始游戏", "仓库", "特战干员", "部门", "交易行", "特勤处")
RESULT_WORDS = {"撤离成功": "撤离成功", "撤离失败": "撤离失败", "带出价值": "", "本局收益": ""}
DEAD_WORDS = ("你已阵亡", "已阵亡", "你被击败", "击败了你", "被淘汰", "正在观战", "观战中")
DOWN_WORDS = ("被击倒", "等待救援", "放弃救援", "等待队友救援", "倒地")
MODE_WORDS = ("烽火地带", "全面战场")

_executor: ThreadPoolExecutor | None = None
_engine = None
_status = "" if sys.platform == "win32" else "只在 Windows 上可用"


def _init_thread() -> None:
    try:
        ctypes.windll.combase.RoInitialize(1)  # RO_INIT_MULTITHREADED
    except (OSError, AttributeError):
        pass


async def _recognize_async(data: bytes) -> str:
    global _engine
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream
    if _engine is None:
        for tag in ("zh-Hans-CN", "zh-Hans", "zh-CN"):
            lang = Language(tag)
            if OcrEngine.is_language_supported(lang):
                _engine = OcrEngine.try_create_from_language(lang)
                break
        if _engine is None:
            _engine = OcrEngine.try_create_from_user_profile_languages()
        if _engine is None:
            raise RuntimeError("系统里没有可用的 OCR 语言包")
    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(data)
    await writer.store_async()
    writer.detach_stream()
    stream.seek(0)
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    result = await _engine.recognize_async(bitmap)
    return "\n".join(line.text for line in result.lines)


def _run(data: bytes) -> str:
    return asyncio.run(_recognize_async(data))


def available() -> bool:
    """第一次调用时试一下：模块装好了、中文识别能用，才算可用。"""
    global _executor, _status
    if _status:
        return False
    if _executor is None:
        try:
            importlib.import_module("winrt.windows.media.ocr")  # 检查组件装好了没有
        except ImportError:
            _status = "没有安装 Windows 文字识别组件（重新运行 setup.bat）"
            log.info("本机文字识别不可用：%s", _status)
            return False
        _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ocr", initializer=_init_thread)
    return True


def status() -> str:
    return _status or ("可用" if _executor is not None else "未检测")


def recognize(data: bytes) -> str:
    """识别一张 JPEG / PNG 里的文字。出错返回空字符串，连续出错就停用。"""
    global _status
    if not data or not available():
        return ""
    try:
        return _executor.submit(_run, data).result(timeout=20)
    except Exception as exc:  # WinRT 的错误类型很多，统一记日志后停用，避免反复报错
        _status = f"文字识别出错，已停用：{type(exc).__name__}: {exc}"[:200]
        log.warning("%s", _status)
        return ""


_SPACES = re.compile(r"\s+")


def classify(text: str) -> tuple[str, dict]:
    """识别出的文字 → (画面状态或空, {"map", "mode", "result", "kw"})。状态为空表示文字里看不出来。
    kw 是命中的关键词（诊断包里统计用，不保存识别出的原文，避免带上昵称等个人信息）。"""
    t = _SPACES.sub("", text or "")  # 中文识别结果字和字之间可能带空格
    info: dict = {}
    for name in MAPS:
        if name in t:
            info["map"] = name
            break
    for word in MODE_WORDS:
        if word in t:
            info["mode"] = word
            break
    lobby = [w for w in LOBBY_WORDS if w in t]
    if len(lobby) >= 2:
        info["kw"] = "+".join(lobby[:2])
        return "lobby", info
    for word, value in RESULT_WORDS.items():
        if word in t:
            if value:
                info["result"] = value
            info["kw"] = word
            return "result", info
    for state, words in (("dead", DEAD_WORDS), ("down", DOWN_WORDS)):
        hit = next((w for w in words if w in t), "")
        if hit:
            info["kw"] = hit
            return state, info
    return "", info
