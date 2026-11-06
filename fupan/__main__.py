import importlib
import sys


def _missing_dependency(name: str) -> None:
    """依赖还没装好时给出明确提示（双击 run.bat 没有黑色窗口，所以同时弹一个对话框）。"""
    text = (f"缺少运行组件：{name}\n\n"
            "请先双击安装文件夹里的 setup.bat，等窗口里出现“安装完成”后，再打开本软件。")
    print(text, file=sys.stderr)
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "三角洲复盘助手", 0x40)


for _name in ("httpx", "keyring", "PySide6.QtWebEngineWidgets"):
    try:
        importlib.import_module(_name)
    except ImportError as exc:
        _missing_dependency(exc.name or _name)
        sys.exit(1)

from .ui.web_host import run  # noqa: E402

sys.exit(run())
