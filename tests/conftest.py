import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def memory_keyring(monkeypatch):
    """测试不碰电脑上真实的凭据管理器 / 钥匙串：密钥只存在内存里。"""
    from fupan import secrets_store
    store: dict[str, str] = {}
    monkeypatch.setattr(secrets_store, "get_api_key", lambda provider: store.get(provider, ""))
    monkeypatch.setattr(secrets_store, "set_api_key", lambda provider, key: store.__setitem__(provider, key.strip()))
    monkeypatch.setattr(secrets_store, "delete_api_key", lambda provider: store.pop(provider, None))
    return store


def pytest_configure(config):
    config.addinivalue_line("markers", "real_ocr: 用电脑上真实的 Windows 文字识别（默认测试里关掉，保证结果和平台无关）")


@pytest.fixture(autouse=True)
def no_host_ocr(request, monkeypatch):
    """默认不用电脑上真实的文字识别，测试结果在 Windows / Mac / Linux 上一致；需要时测试里自己替换。"""
    if request.node.get_closest_marker("real_ocr"):
        return
    from fupan import ocr
    monkeypatch.setattr(ocr, "available", lambda: False)
