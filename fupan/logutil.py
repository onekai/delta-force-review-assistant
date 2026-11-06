"""日志：写到本地文件，自动把 API Key 一类的字符串替换成 ***。"""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler

from . import paths

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9_\-\.]{8,}"),
    re.compile(r"(?i)(api[_-]?key[\"']?\s*[:=]\s*[\"']?)[A-Za-z0-9_\-\.]{8,}"),
]


def redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda m: m.group(1) + "***", text)
        else:
            text = pattern.sub("***", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True


def setup(level: int = logging.INFO) -> None:
    handler = RotatingFileHandler(paths.log_dir() / "app.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    console.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers[:] = [handler, console]
