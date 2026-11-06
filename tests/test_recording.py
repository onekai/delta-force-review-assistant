from fupan import encoders, recorder
from fupan.config import Config
from fupan.game_watch import GameState, find_game_window
from fupan.sessions import read_session
from fupan.winapi import MonitorInfo, Rect, WindowInfo

MON = MonitorInfo(0, Rect(0, 0, 1920, 1080), True, r"\\.\DISPLAY1")


def win(title="三角洲行动", exe="DeltaForce.exe", rect=Rect(0, 0, 1920, 1080), hwnd=1, visible=True, minimized=False):
    return WindowInfo(hwnd, title, 100, exe, rect, visible, minimized)


# ---- 游戏识别 ----
def test_find_by_title_and_size():
    cfg = Config()
    small = win(rect=Rect(0, 0, 800, 600), hwnd=2)
    other = win(title="微信", exe="WeChat.exe", hwnd=3)
    assert find_game_window([small, other], cfg) is None
    assert find_game_window([small, other, win()], cfg).hwnd == 1


def test_process_name_takes_priority_over_title():
    cfg = Config(game_process_names=["DeltaForceClient.exe"])
    launcher = win(exe="Launcher.exe", hwnd=5)
    game = win(exe="DeltaForceClient.exe", hwnd=6, title="")
    assert find_game_window([launcher, game], cfg).hwnd == 6
    assert find_game_window([launcher], cfg) is None


def test_minimized_window_ignored():
    assert find_game_window([win(minimized=True)], Config()) is None


# ---- 编码命令 ----
def test_candidate_order_by_vendor():
    assert encoders.candidate_order(["NVIDIA GeForce RTX 4070"])[0] == "h264_nvenc"
    assert encoders.candidate_order(["AMD Radeon RX 7800 XT"])[0] == "h264_amf"
    assert encoders.candidate_order(["Intel(R) UHD Graphics 770"])[0] == "h264_qsv"
    order = encoders.candidate_order([])
    assert order[-1] == "h264_mf" and len(order) == len(set(order))


def test_record_command_has_crop_and_segments(tmp_path):
    crop = encoders.Crop(10, 20, 1281, 721)
    cmd = encoders.record_command("ffmpeg", "h264_qsv", 1, crop, 60, 20, 300, tmp_path / "run01_%03d.mkv")
    src = cmd[cmd.index("-i") + 1]
    assert src.startswith("ddagrab=output_idx=1:framerate=60")
    assert "offset_x=10" in src and "video_size=1280x720" in src
    assert cmd[cmd.index("-vf") + 1] == "hwmap=derive_device=qsv,format=qsv"
    assert "-segment_time" in cmd and cmd[cmd.index("-segment_time") + 1] == "300"
    assert cmd[cmd.index("-b:v") + 1] == "20M"


def test_nvenc_has_no_filter():
    cmd = encoders.record_command("ffmpeg", "h264_nvenc", 0, encoders.Crop(0, 0, 1920, 1080), 60, 20, 300, "x.mkv")
    assert "-vf" not in cmd


def test_compute_crop_clamps_to_monitor():
    crop = recorder.compute_crop(Rect(-8, -8, 1928, 1088), MON.rect)
    assert (crop.x, crop.y, crop.width, crop.height) == (0, 0, 1920, 1080)
    second = MonitorInfo(1, Rect(1920, 0, 4480, 1440), False, "d2")
    crop2 = recorder.compute_crop(Rect(1920, 0, 4480, 1440), second.rect)
    assert (crop2.x, crop2.width, crop2.height) == (0, 2560, 1440)


# ---- 录制状态机 ----
class FakeProc:
    instances = []

    def __init__(self, cmd, log_file):
        self.cmd = cmd
        self.log_file = log_file
        self.stats = {}
        self._alive = True
        self.reason = "error"
        self.proc = type("P", (), {"returncode": None})()
        FakeProc.instances.append(self)

    def alive(self):
        return self._alive

    def exit_reason(self):
        return self.reason

    def die(self, reason="error", code=1):
        self._alive = False
        self.reason = reason
        self.proc.returncode = code

    def stop(self, timeout=8.0):
        self._alive = False
        self.proc.returncode = 0
        return 0


def make_controller(tmp_path, monkeypatch, **cfg_kwargs):
    FakeProc.instances = []
    monkeypatch.setattr(recorder, "FFmpegProcess", FakeProc)
    monkeypatch.setattr(recorder, "free_gb", lambda folder: 100.0)
    cfg = Config(recordings_dir=str(tmp_path), **cfg_kwargs)
    ctl = recorder.RecordingController(cfg, ffmpeg="ffmpeg")
    ctl.encoder = "h264_nvenc"
    ctl.monitor_provider = lambda: [MON]
    return ctl


def test_starts_after_foreground_delay_and_pauses_in_background(tmp_path, monkeypatch):
    ctl = make_controller(tmp_path, monkeypatch)
    fg = GameState(win(), True)
    bg = GameState(win(), False)
    ctl.tick(fg, now=100.0)
    assert not FakeProc.instances and "即将" in ctl.status
    ctl.tick(fg, now=102.5)
    assert len(FakeProc.instances) == 1 and ctl.recording
    ctl.tick(bg, now=103.0)
    assert ctl.recording  # 切后台不到 3 秒还在录
    ctl.tick(bg, now=106.5)
    assert not ctl.recording and "暂停" in ctl.status
    ctl.tick(fg, now=107.0)
    ctl.tick(fg, now=109.5)
    assert len(FakeProc.instances) == 2
    assert len(ctl.session.runs) == 2


def test_game_exit_closes_session(tmp_path, monkeypatch):
    ctl = make_controller(tmp_path, monkeypatch)
    fg = GameState(win(), True)
    ctl.tick(fg, now=100.0)
    ctl.tick(fg, now=103.0)
    folder = ctl.session_dir
    ctl.tick(GameState(None, False), now=200.0)
    assert ctl.session is None and not ctl.recording
    saved = read_session(folder)
    assert saved["ended_at"] == 200.0 and saved["runs"][0]["ended_at"] == 200.0


def test_window_resize_restarts_run(tmp_path, monkeypatch):
    ctl = make_controller(tmp_path, monkeypatch)
    ctl.tick(GameState(win(), True), now=100.0)
    ctl.tick(GameState(win(), True), now=103.0)
    ctl.tick(GameState(win(rect=Rect(0, 0, 1280, 720)), True), now=104.0)
    assert len(FakeProc.instances) == 2
    assert "video_size=1280x720" in FakeProc.instances[1].cmd[FakeProc.instances[1].cmd.index("-i") + 1]


def test_output_changed_resumes_quickly_and_never_gives_up(tmp_path, monkeypatch):
    """游戏切换全屏时 FFmpeg 报 Output changed 退出：1 秒后续录，不算失败，多少次都继续。"""
    ctl = make_controller(tmp_path, monkeypatch)
    fg = GameState(win(), True)
    ctl.tick(fg, now=100.0)
    ctl.tick(fg, now=103.0)
    t = 103.0
    for _ in range(8):
        ctl.proc.die("output_changed", code=0)
        t += 1
        ctl.tick(fg, now=t)  # 发现退出，安排 1 秒后续录
        assert ctl.proc is None and "画面切换" in ctl.status
        t += 1.1
        ctl.tick(fg, now=t)
        assert ctl.recording
    assert len(FakeProc.instances) == 9 and ctl.output_changes == 8 and ctl.failures == 0
    assert [r.reason for r in ctl.session.runs[:8]] == ["output_changed"] * 8


def test_real_errors_back_off_but_keep_trying(tmp_path, monkeypatch):
    ctl = make_controller(tmp_path, monkeypatch)
    fg = GameState(win(), True)
    ctl.tick(fg, now=100.0)
    ctl.tick(fg, now=103.0)
    t = 103.0
    waits = []
    for _ in range(7):
        ctl.proc.die("error", code=1)
        t += 0.5
        ctl.tick(fg, now=t)
        start = t
        while ctl.proc is None:
            t += 0.5
            ctl.tick(fg, now=t)
        waits.append(round(t - start))
    assert waits[:5] == [2, 4, 8, 16, 32] and waits[5:] == [60, 60]  # 最长 60 秒一次，游戏开着就一直重试
    assert ctl.failures == 7 and "退出码 1" in ctl.error


def test_waits_for_window_to_settle(tmp_path, monkeypatch):
    ctl = make_controller(tmp_path, monkeypatch)
    ctl.tick(GameState(win(rect=Rect(0, 0, 1280, 720)), True), now=100.0)
    ctl.tick(GameState(win(), True), now=102.5)  # 前台满 2 秒，但窗口刚变大
    assert not FakeProc.instances
    ctl.tick(GameState(win(), True), now=103.6)
    assert len(FakeProc.instances) == 1


def test_no_encoder_or_ffmpeg_blocks_recording(tmp_path, monkeypatch):
    ctl = make_controller(tmp_path, monkeypatch)
    ctl.encoder = ""
    ctl.tick(GameState(win(), True), now=100.0)
    ctl.tick(GameState(win(), True), now=103.0)
    assert not FakeProc.instances and "编码器" in ctl.status
    ctl.ffmpeg = ""
    ctl.tick(GameState(win(), True), now=104.0)
    assert "FFmpeg" in ctl.status


def test_toggle_off_stops(tmp_path, monkeypatch):
    ctl = make_controller(tmp_path, monkeypatch)
    ctl.tick(GameState(win(), True), now=100.0)
    ctl.tick(GameState(win(), True), now=103.0)
    ctl.cfg.auto_record = False
    ctl.tick(GameState(win(), True), now=104.0)
    assert not ctl.recording and "关闭" in ctl.status
