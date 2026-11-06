"""API Key 存取。Windows 上存在“凭据管理器”里，不写进配置文件、日志或诊断包。"""

from __future__ import annotations

import logging

from . import APP_ID

log = logging.getLogger(__name__)

try:
    import keyring
    from keyring.errors import KeyringError
except ImportError:  # 依赖没装好时程序仍能启动，只是不能保存密钥
    keyring = None
    KeyringError = Exception


def _username(provider: str) -> str:
    return f"{provider}_api_key"


def get_api_key(provider: str) -> str:
    if keyring is None:
        return ""
    try:
        return keyring.get_password(APP_ID, _username(provider)) or ""
    except KeyringError as exc:
        log.warning("读取密钥失败：%s", type(exc).__name__)
        return ""


def set_api_key(provider: str, key: str) -> None:
    if keyring is None:
        raise RuntimeError("密钥存储组件没有安装，请重新运行安装脚本")
    keyring.set_password(APP_ID, _username(provider), key.strip())


def delete_api_key(provider: str) -> None:
    if keyring is None:
        return
    try:
        keyring.delete_password(APP_ID, _username(provider))
    except KeyringError:
        pass


def mask(key: str) -> str:
    """界面上只显示后 4 位。"""
    if not key:
        return "未设置"
    return "已保存（尾号 " + key[-4:] + "）" if len(key) > 8 else "已保存"
