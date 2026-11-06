"""桌面外壳：用 Qt 自带的网页组件显示 fupan/web 里的界面，通过 QWebChannel 调用本机后台。

- 界面文件全在本地，不联网加载（改枪码封面图除外，来自 B 站图床）。
- 界面调用后台用“请求编号 + 回复信号”的方式：慢的操作在后台线程执行，界面不会卡住。
- 关窗口时如果自动录制开着，就缩到右下角托盘继续工作；窗口隐藏时网页暂停运行，不占 CPU。
- 同一时间只运行一个：再次双击快捷方式会把已经打开的窗口调到前面。
"""

from __future__ import annotations

import ctypes
import getpass
import json
import logging
import os
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QFile, QIODevice, QObject, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QColor, QDesktopServices, QGuiApplication, QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineScript, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QFileDialog, QMainWindow, QMenu, QSystemTrayIcon

from .. import APP_ID, APP_NAME, __version__, logutil
from ..backend import DIALOGS, THREADED, Backend, Host

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
VIDEO_FILTER = "视频文件 (*.mp4 *.mkv *.mov *.avi *.flv *.webm *.m4v *.ts)"
IMAGE_FILTER = "图片 (*.png *.jpg *.jpeg *.bmp *.webp)"


def app_icon() -> QIcon:
    ico = WEB_DIR / "img" / "app.ico"
    return QIcon(str(ico if ico.exists() else WEB_DIR / "img" / "logo.svg"))


class QtHost(Host):
    def open_path(self, path: str) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def open_url(self, url: str) -> None:
        QDesktopServices.openUrl(QUrl(url))

    def copy_text(self, text: str) -> None:
        QGuiApplication.clipboard().setText(text)


class Bridge(QObject):
    """网页里的 backend 对象。request(编号, 方法, 参数) → 稍后发出 reply(编号, 结果)。"""

    reply = Signal(int, str)
    _finished = Signal(int, str)

    def __init__(self, backend: Backend, window: "MainWindow"):
        super().__init__()
        self.backend = backend
        self.window = window
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ui-call")
        self._finished.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    @Slot(int, str)
    def _deliver(self, rid: int, payload: str) -> None:
        self.reply.emit(rid, payload)

    @staticmethod
    def _dump(result: dict) -> str:
        return json.dumps(result, ensure_ascii=False, default=str)

    @Slot(int, str, str)
    def request(self, rid: int, method: str, args_json: str) -> None:
        try:
            args = json.loads(args_json or "{}")
        except ValueError:
            args = {}
        if not isinstance(args, dict):
            args = {}
        dialog = DIALOGS.get(method) or DIALOGS.get(f"{method}:{args.get('kind', '')}")
        if dialog:
            path = self.window.ask_path(*dialog)
            if not path:
                self.reply.emit(rid, self._dump({"ok": True, "data": {"text": "没有选择文件"} if method == "ai_test" else {}}))
                return
            args["path"] = path
        if method in THREADED:
            self.pool.submit(self._run, rid, method, args)
        else:
            self.reply.emit(rid, self._dump(self.backend.call(method, args)))

    def _run(self, rid: int, method: str, args: dict) -> None:
        try:
            result = self.backend.call(method, args)
        except Exception as exc:  # backend.call 已经兜底，这里只防万一
            log.exception("后台调用出错")
            result = {"ok": False, "error": str(exc)}
        self._finished.emit(rid, self._dump(result))


class Page(QWebEnginePage):
    def __init__(self, profile: QWebEngineProfile, parent=None):
        super().__init__(profile, parent)

    def acceptNavigationRequest(self, url: QUrl, nav_type, is_main_frame: bool) -> bool:
        # 只允许显示本地的界面文件；其他跳转一律拦下（外部链接由后台用系统浏览器打开）
        return url.isLocalFile() and Path(url.toLocalFile()).name == "index.html"

    def createWindow(self, _type):
        return None

    def javaScriptConsoleMessage(self, level, message: str, line: int, source: str) -> None:
        if level != QWebEnginePage.JavaScriptConsoleMessageLevel.InfoMessageLevel:
            log.warning("界面脚本：%s（%s:%d）", message, Path(QUrl(source).path()).name, line)


def _dark_title_bar(window) -> None:
    if sys.platform != "win32":
        return
    try:
        hwnd = int(window.winId())
        dwm = ctypes.WinDLL("dwmapi")
        on = ctypes.c_int(1)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), ctypes.sizeof(on))  # 深色标题栏
        color = ctypes.c_uint(0x00160F06)  # 标题栏颜色 #060f16（Windows 11）
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(color), ctypes.sizeof(color))
    except (OSError, AttributeError):
        pass


class MainWindow(QMainWindow):
    def __init__(self, backend: Backend, debug: bool = False):
        super().__init__()
        self.backend = backend
        self._quitting = False
        self._tray_hinted = False
        self._ticks = 0
        self.setWindowTitle(f"{APP_NAME} v{__version__} 测试版")
        self.setWindowIcon(app_icon())
        self.setMinimumSize(1180, 760)
        avail = QGuiApplication.primaryScreen().availableGeometry()
        self.resize(min(1440, int(avail.width() * 0.94)), min(900, int(avail.height() * 0.92)))

        # 无痕配置：不在硬盘上留网页缓存和 Cookie。挂在应用上，保证比网页晚释放
        self.profile = QWebEngineProfile(QApplication.instance())
        self.page = Page(self.profile, self)
        s = self.page.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)
        s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False)
        self.page.setBackgroundColor(QColor("#0b141c"))
        self._inject_webchannel_js()
        self.channel = QWebChannel(self.page)
        self.bridge = Bridge(backend, self)
        self.channel.registerObject("backend", self.bridge)
        self.page.setWebChannel(self.channel)

        self.view = QWebEngineView(self)
        self.view.setPage(self.page)
        if not debug:
            self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.setCentralWidget(self.view)
        self.view.load(QUrl.fromLocalFile(str(WEB_DIR / "index.html")))

        self._build_tray()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(1000)

    def _inject_webchannel_js(self) -> None:
        f = QFile(":/qtwebchannel/qwebchannel.js")
        if not f.open(QIODevice.OpenModeFlag.ReadOnly):
            log.error("找不到 qwebchannel.js，界面无法连接后台")
            return
        script = QWebEngineScript()
        script.setName("qwebchannel")
        script.setSourceCode(bytes(f.readAll()).decode("utf-8"))
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(False)
        self.page.scripts().insert(script)

    # ---- 托盘 ----
    def _build_tray(self) -> None:
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(app_icon(), self)
        menu = QMenu(self)
        show = QAction("打开主界面", self)
        show.triggered.connect(self.show_normal)
        self.act_record = QAction("自动录制", self)
        self.act_record.setCheckable(True)
        self.act_record.triggered.connect(lambda on: self.backend.call("set_auto_record", {"on": bool(on)}))
        quit_ = QAction("退出", self)
        quit_.triggered.connect(self.quit)
        menu.addAction(show)
        menu.addAction(self.act_record)
        menu.addSeparator()
        menu.addAction(quit_)
        menu.aboutToShow.connect(lambda: self.act_record.setChecked(self.backend.cfg.auto_record))
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self.show_normal() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.setToolTip(APP_NAME)
        self.tray.show()

    def show_normal(self) -> None:
        self._set_active(True)
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _set_active(self, active: bool) -> None:
        try:
            state = QWebEnginePage.LifecycleState.Active if active else QWebEnginePage.LifecycleState.Frozen
            if self.page.lifecycleState() != state:
                self.page.setLifecycleState(state)
        except (AttributeError, RuntimeError):
            pass

    # ---- 定时 ----
    def _tick(self) -> None:
        self.backend.tick()
        self._ticks += 1
        if self.tray is not None and self._ticks % 5 == 0:
            self.tray.setToolTip(f"{APP_NAME} · {self.backend.controller.status}"[:120])

    # ---- 文件选择 ----
    def ask_path(self, kind: str, title: str) -> str:
        if kind == "folder":
            return QFileDialog.getExistingDirectory(self, title, str(self.backend.cfg.recordings_path()))
        start = str(Path.home() / ("Videos" if kind == "video" else "Pictures"))
        path, _ = QFileDialog.getOpenFileName(self, title, start, VIDEO_FILTER if kind == "video" else IMAGE_FILTER)
        return path

    # ---- 显示与关闭 ----
    def showEvent(self, event) -> None:
        super().showEvent(event)
        _dark_title_bar(self)
        self._set_active(True)

    def closeEvent(self, event) -> None:
        if not self._quitting and self.tray is not None and self.backend.cfg.auto_record:
            event.ignore()
            self.hide()
            self._set_active(False)
            if not self._tray_hinted:
                self._tray_hinted = True
                self.tray.showMessage(APP_NAME, "已缩到右下角托盘，游戏启动时会自动录制。右键托盘图标可以退出。",
                                      QSystemTrayIcon.MessageIcon.Information, 4000)
            return
        event.accept()
        self.quit()

    def quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self.timer.stop()
        try:
            self.backend.shutdown()
        finally:
            self.bridge.pool.shutdown(wait=False, cancel_futures=True)
            if self.tray is not None:
                self.tray.hide()
            QApplication.quit()


def _server_name() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # 拿不到用户名时用固定名字
        user = "user"
    return f"{APP_ID}-{user}"


def _signal_running_instance() -> bool:
    sock = QLocalSocket()
    sock.connectToServer(_server_name())
    if not sock.waitForConnected(400):
        return False
    sock.write(b"show")
    sock.flush()
    sock.waitForBytesWritten(400)
    sock.disconnectFromServer()
    return True


def _screenshot_tour(window: MainWindow, out_dir: Path, views: list[str]) -> None:
    """测试用：依次打开每个页面并截图，然后退出。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    steps = list(views)

    def shoot(view: str) -> None:
        window.view.grab().save(str(out_dir / f"{view}.png"))
        window.page.runJavaScript("document.querySelector('#view') ? document.querySelector('#view').innerText.slice(0, 400) : ''",
                                  0, lambda text, v=view: log.info("页面 %s 文字：%s", v, (text or "").replace("\n", " | ")))
        nxt()

    def nxt() -> None:
        if not steps:
            QTimer.singleShot(800, window.quit)
            return
        view = steps.pop(0)
        window.page.runJavaScript(f"location.hash = '#{view}';")
        QTimer.singleShot(3500, lambda: shoot(view))
    QTimer.singleShot(4000, nxt)


def run(argv: list[str] | None = None) -> int:
    argv = list(argv or sys.argv)
    debug = "--debug" in argv
    shots = argv[argv.index("--screenshots") + 1] if "--screenshots" in argv else ""
    if debug:
        os.environ.setdefault("QTWEBENGINE_REMOTE_DEBUGGING", "127.0.0.1:9223")
    logutil.setup(logging.DEBUG if debug else logging.INFO)
    sys.excepthook = lambda t, v, tb: log.error("未处理的错误：%s", "".join(traceback.format_exception(t, v, tb)))
    log.info("%s %s 启动", APP_NAME, __version__)
    if sys.platform == "win32":  # 任务栏显示本软件的图标，而不是 Python 的
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"{APP_ID}.App")
        except (OSError, AttributeError):
            pass
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(app_icon())
    if not shots and _signal_running_instance():
        log.info("已经在运行，切换到已打开的窗口")
        return 0
    backend = Backend(host=QtHost())
    backend.start()
    window = MainWindow(backend, debug=debug)
    server = QLocalServer(window)
    if not shots:
        QLocalServer.removeServer(_server_name())
        server.listen(_server_name())

        def on_connection() -> None:
            sock = server.nextPendingConnection()
            if sock is not None:
                sock.readyRead.connect(lambda: (sock.readAll(), window.show_normal()))
        server.newConnection.connect(on_connection)
    window.show()
    if shots:
        _screenshot_tour(window, Path(shots), ["overview", "matches", "gunsmith", "optimize", "settings"])
    code = app.exec()
    backend.shutdown()
    return code
