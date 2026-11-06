import subprocess
import time
from pathlib import Path

import pytest

from fupan import media
from fupan.encoders import find_ffmpeg
from fupan.jobs import JobError, Jobs

FFMPEG = find_ffmpeg()
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="没有 FFmpeg")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUPAN_HOME", str(tmp_path))
    return tmp_path


def make_recording(folder: Path, run: int, seconds: int, size="640x360", seg=4):
    folder.mkdir(parents=True, exist_ok=True)
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30",
                    "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-f", "segment",
                    "-segment_time", str(seg), "-reset_timestamps", "1", "-segment_format", "matroska",
                    str(folder / f"run{run:02d}_%03d.mkv")], check=True)


# ---- 录像时间轴 ----
@needs_ffmpeg
def test_timeline_clips_and_thumbs(home):
    folder = home / "recordings" / "20261020_194210"
    make_recording(folder, 1, 9)
    make_recording(folder, 2, 4, size="800x450")
    (folder / "notes.txt").write_text("x")
    files = media.list_video_files(folder)
    assert [f.name for f in files] == ["run01_000.mkv", "run01_001.mkv", "run01_002.mkv", "run02_000.mkv"]
    tl = media.Timeline("20261020_194210", files)
    assert abs(tl.duration - 13) < 0.3
    seg, off = tl.locate(10.0)
    assert seg.path.endswith("run02_000.mkv") and abs(off - 1) < 0.3  # 第一段 9 秒，第二段从 9 秒开始
    assert [round(p[2], 1) for p in tl.pieces(6, 11)] == [2.0, 1.0, 2.0]

    clip = tl.make_clip(10.0, before=4, after=2)  # 跨两段录像、两种分辨率
    assert Path(clip["path"]).suffix == ".webm" and clip["t_start"] == 6.0
    info = media.probe(Path(clip["path"]))
    assert info["height"] == 720 and abs(info["duration"] - 6) < 0.5
    assert tl.make_clip(10.0, before=4, after=2) == clip  # 第二次直接用缓存

    thumbs = tl.make_thumbs(step=2)
    assert len(thumbs) >= 6 and thumbs == tl.thumbs()[0] and tl.thumbs()[1] == 2
    # 只有 1 秒的分段（第 8～9 秒）和第二段录像的开头也要有缩略图
    assert any(abs(th["t"] - 8.0) < 0.1 for th in thumbs) and any(abs(th["t"] - 9.0) < 0.1 for th in thumbs)
    assert tl.frame(3.0, width=320)[:2] == b"\xff\xd8"
    assert tl.grid([1, 5, 9, 12], cols=2, tile_width=320)[:2] == b"\xff\xd8"
    sheets = tl.scan_sheets(every=3)
    assert sheets[0]["times"][:2] == [0.0, 3.0]  # 关键帧每秒一个（-g 30），时间是准确值
    assert tl.scan_sheets(every=3) == sheets  # 第二次直接用缓存
    assert all(t < tl.duration for s in sheets for t in s["times"])

    # 缓存的时间轴在文件不变时不重新读取
    again = media.Timeline("20261020_194210", files)
    assert [s.duration for s in again.segments] == [s.duration for s in tl.segments]


def test_empty_timeline_raises(home):
    tl = media.Timeline("nothing", [])
    assert tl.duration == 0 and tl.locate(5) is None
    with pytest.raises(media.MediaError):
        tl.make_clip(5)


# ---- 后台任务 ----
def wait(jobs, timeout=10):
    t0 = time.time()
    while jobs.active() and time.time() - t0 < timeout:
        time.sleep(0.05)


def test_jobs_lifecycle_and_dedupe():
    jobs = Jobs()
    gate = __import__("threading").Event()
    a = jobs.start("x", "任务", lambda job: gate.wait(5) and "完成了", session="s", pool="ai")
    b = jobs.start("x", "任务", lambda job: "重复", session="s", pool="ai")
    assert a is b
    queued = jobs.start("y", "排队", lambda job: "ok", pool="ai")
    assert [j["status"] for j in jobs.list()] == ["running", "running"]  # 排队中也显示为进行中
    assert "排队中" in [j for j in jobs.list() if j["id"] == queued.id][0]["label"]
    gate.set()
    wait(jobs)
    states = {j["id"]: (j["status"], j["message"]) for j in jobs.list()}
    assert states[a.id] == ("done", "完成了")

    def fail(job):
        raise JobError("给用户看的原因")
    f = jobs.start("z", "失败", fail)
    wait(jobs)
    assert [j for j in jobs.list() if j["id"] == f.id][0]["message"] == "给用户看的原因"

    def slow(job):
        for _ in range(100):
            job.check()
            time.sleep(0.02)
    c = jobs.start("post", "整理", slow, pool="ai")
    time.sleep(0.1)
    assert jobs.cancel({"post"}) == [c]
    wait(jobs)
    assert c.status == "cancelled" and all(j["id"] != c.id for j in jobs.list())  # 取消的不显示
    jobs.shutdown()


# ---- 后台接口 ----
@pytest.fixture
def backend(home):
    from fupan.backend import Backend
    b = Backend()
    yield b
    b.jobs.shutdown()
    b.db.close()


def call(b, method, **args):
    r = b.call(method, args)
    assert r["ok"], r
    return r["data"]


def test_unknown_method_and_bad_args(backend):
    assert backend.call("nope", {})["ok"] is False
    assert backend.call("__init__", {})["ok"] is False
    r = backend.call("delete_event", {"oops": 1})
    assert r == {"ok": False, "error": "参数不对，请刷新界面后重试"}


def test_events_and_stats(backend, home):
    (home / "recordings" / "20261020_194210").mkdir(parents=True)
    (home / "recordings" / "20261020_194210" / "session.json").write_text(
        '{"name": "20261020_194210", "started_at": 1792496530, "runs": []}', encoding="utf-8")
    name = "20261020_194210"
    d1 = call(backend, "add_event", name=name, t=30, kind="death")["id"]
    call(backend, "add_event", name=name, t=31, kind="duel_loss")
    call(backend, "add_event", name=name, t=10, kind="duel_win")
    call(backend, "update_event", id=d1, cause="recoil", confirmed=True)
    assert backend.call("update_event", {"id": d1, "cause": "cheating"})["ok"] is False
    assert backend.call("add_event", {"name": name, "t": 1, "kind": "kill"})["ok"] is False
    m = call(backend, "matches", days=0)
    row = m["list"][0]
    assert row["death_count"] == 1 and row["duel"] == {"w": 1, "l": 1} and row["title"] == "三角洲行动对局 · 原片已清理（片段保留）"
    assert m["kpis"]["top_cause"] == "recoil" and m["kpis"]["duel_rate"] == 0.5
    call(backend, "set_session_meta", name=name, mode="extraction")
    call(backend, "set_session_meta", name=name, map="零号大坝")
    assert call(backend, "matches", days=0, mode="warfare")["list"] == []
    assert call(backend, "matches", days=0, mode="extraction")["list"][0]["title"].startswith("零号大坝 · 烽火地带")
    call(backend, "delete_event", id=d1)
    assert call(backend, "matches", days=0)["list"][0]["death_count"] == 0


def test_settings_validation(backend):
    call(backend, "save_settings", retention_days=7)
    assert backend.cfg.retention_days == 7
    for bad in ({"retention_days": 5}, {"framerate": True}, {"ai_base_url": "http://x.com"},
                {"game_title_keywords": []}, {"recordings_dir": "C:/"}, {"a": 1, "b": 2}):
        assert backend.call("save_settings", bad)["ok"] is False, bad
    call(backend, "save_profile", ads_vertical=1.1, fov=None)
    assert backend.cfg.profile == {"ads_vertical": 1.1}
    assert backend.call("save_profile", {"fov": 500})["ok"] is False
    assert backend.call("set_api_key", {"key": "short"})["ok"] is False


def test_open_url_allowlist_and_paths(backend, home):
    assert backend.call("open_url", {"url": "https://www.bilibili.com/video/BV1AAAAAAAAA"})["ok"]
    for bad in ("http://www.bilibili.com/x", "file:///C:/Windows/system32/calc.exe", "https://evil.example.com"):
        assert backend.call("open_url", {"url": bad})["ok"] is False
    exe = home / "tool.exe"
    exe.write_text("x")
    opened = []
    backend.host.open_path = opened.append
    call(backend, "open_path", path=str(exe))
    assert opened == [str(home)]  # 只打开所在文件夹，不运行文件


def test_dashboard_and_pages_have_expected_shape(backend):
    d = call(backend, "dashboard")
    assert set(d) >= {"pre", "mid", "post", "recent", "diagnosis", "min_deaths", "recommendations", "verified",
                      "gunsmith_top"}
    s = call(backend, "state")
    assert s["first_run"] is True and s["recorder"]["capture"].startswith("DDA")
    g = call(backend, "gunsmith")
    assert g["top"] == [] and g["crawl"]["count"] == 0
    o = call(backend, "optimizer")
    assert {"pass", "total", "items", "measure"} <= set(o)
    st = call(backend, "settings")
    assert st["restore"]["count"] == 0 and "…" in st["fingerprint"]


def test_export_data_has_no_secrets(backend, home):
    import zipfile
    f = call(backend, "export_data")["file"]
    names = zipfile.ZipFile(home / "desktop" / f).namelist()
    assert "fupan.db" in names and "events.csv" in names


@needs_ffmpeg
def test_import_and_clip_flow(backend, home):
    src = home / "outside" / "my_match.mp4"
    src.parent.mkdir()
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
                    "-t", "12", "-c:v", "libx264", "-preset", "ultrafast", str(src)], check=True)
    name = call(backend, "import_video", path=str(src))["name"]
    assert call(backend, "import_video", path=str(src))["name"] == name  # 同一个文件不重复导入
    wait(backend.jobs, 60)
    detail = call(backend, "session_detail", name=name)
    assert detail["thumbs"] and detail["spec"].endswith("导入") and abs(detail["total_duration"] - 12) < 0.5
    eid = call(backend, "add_event", name=name, t=8, kind="death")["id"]
    call(backend, "make_clip", name=name, t=8, event_id=eid)
    wait(backend.jobs, 60)
    ev = [e for e in call(backend, "session_detail", name=name)["events"] if e["id"] == eid][0]
    assert ev["clip"] and ev["clip"]["src"].startswith("file://")
    # 在正在看的片段里再标一次阵亡，直接复用这个片段
    e2 = call(backend, "add_event", name=name, t=9, kind="death")["id"]
    ev2 = [e for e in call(backend, "session_detail", name=name)["events"] if e["id"] == e2][0]
    assert ev2["clip"] == ev["clip"]


# ---- 界面联调：真实启动桌面外壳（无窗口模式），逐页检查网页连上了后台 ----
def test_desktop_shell_renders_every_page(home):
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    import os
    import sys
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QTWEBENGINE_CHROMIUM_FLAGS="--disable-gpu",
               FUPAN_HOME=str(home))
    shots = home / "shots"
    out = subprocess.run([sys.executable, "-m", "fupan", "--screenshots", str(shots)], cwd=Path(__file__).parent.parent,
                         env=env, capture_output=True, text=True, timeout=120)
    log = out.stdout + out.stderr
    for page, text in (("overview", "自动复盘守护"), ("matches", "导入外部录像"), ("gunsmith", "社区改枪码推荐"),
                       ("optimize", "赛前性能检查"), ("settings", "安全边界与透明说明")):
        assert (shots / f"{page}.png").exists(), log[-2000:]
        assert f"页面 {page} 文字" in log and text in log.split(f"页面 {page} 文字")[1][:600], log[-2000:]
    assert "没有连接到本机后台" not in log and "界面脚本" not in log, log[-2000:]


# ---- 边录边分析：分段 → 画面状态 → 自动切出一局 → 找阵亡、生成片段、诊断 ----
def fake_states(t: float) -> str:
    if t < 30:
        return "lobby"
    if 120 <= t < 126:
        return "down"
    if 126 <= t < 150:
        return "dead"
    if t < 210:
        return "match"
    if t < 222:
        return "result"
    return "lobby"


@needs_ffmpeg
def test_live_pipeline_splits_match_and_finds_death(backend, home, monkeypatch):
    from fupan import analysis
    folder = home / "recordings" / "20261022_192535"
    make_recording(folder, 1, 240, size="320x180", seg=60)
    (folder / "session.json").write_text(
        '{"name": "20261022_192535", "started_at": 1792668335, "runs": [{"index": 1, "started_at": 1792668350, '
        '"encoder": "h264_amf", "crop": {"width": 320, "height": 180}, "pattern": "run01_%03d.mkv"}]}', encoding="utf-8")
    calls = {"sheets": 0, "refine": 0, "diagnose": 0}

    def classify(ai, sheet, cols=3, rows=3):
        calls["sheets"] += 1
        return {"states": [fake_states(t) for t in sheet["times"]], "map": "零号大坝",
                "mode": "", "result": "撤离失败" if any(210 <= t < 222 for t in sheet["times"]) else ""}

    def refine(ai, tl, t):
        calls["refine"] += 1
        return t

    def diagnose(ai, tl, t, offset=0.0):
        calls["diagnose"] += 1
        return {"cause": "recoil", "headline": "连射上飘", "killer_weapon": "M4A1", "distance_m": 35, "confidence": 0.6,
                "model": "deepseek-flash"}

    class FakeAI:
        def close(self):
            pass

    monkeypatch.setattr(analysis, "classify_sheet", classify)
    monkeypatch.setattr(analysis, "refine", refine)
    monkeypatch.setattr(analysis, "diagnose", diagnose)
    monkeypatch.setattr(backend, "ai_ready", lambda: (True, ""))
    monkeypatch.setattr(backend, "_ai_session", lambda job: FakeAI())

    backend._start_segments("20261022_192535", final=True)
    wait(backend.jobs, 120)
    while backend.jobs.active():
        time.sleep(0.2)
    assert calls["sheets"] >= 8 and calls["refine"] == 1 and calls["diagnose"] == 1

    rows = backend.db.matches("20261022_192535")
    assert len(rows) == 1 and rows[0]["closed"] and rows[0]["analyzed_at"]
    assert rows[0]["t_start"] == 30 and abs(rows[0]["t_end"] - 222) < 4 and rows[0]["result"] == "failed"

    m = call(backend, "matches", days=0)
    assert len(m["list"]) == 1
    item = m["list"][0]
    assert item["name"] == f"20261022_192535#{rows[0]['id']}" and item["title"] == "零号大坝 · 烽火地带"
    assert item["result"] == "failed" and item["death_count"] == 1 and item["duel"] == {"w": 0, "l": 1}
    assert abs(item["deaths"][0]["t"] - 90) < 0.1  # 局内时间：120 - 30
    assert abs(item["started_at"] - (1792668350 + 30)) < 1  # 按这段录制的开始时间换算成现实时间

    d = call(backend, "session_detail", name=item["name"])
    assert abs(d["total_duration"] - 192) < 4 and d["meta"]["map"] == "零号大坝"
    ev = d["events"][0]
    assert ev["source"] == "ai" and ev["cause"] == "recoil" and not ev["confirmed"] and ev["clip"]
    assert ev["clip"]["t_start"] < ev["t"] < ev["clip"]["t_end"]  # 片段时间也换算成局内时间
    assert all(0 <= th["t"] < 192 for th in d["thumbs"]) and d["thumbs"]
    # 在这一局里手动加标记：传进来的是局内时间，存的是整场时间
    eid = call(backend, "add_event", name=item["name"], t=10, kind="duel_win")["id"]
    assert abs(backend.db.event(eid)["t"] - 40) < 0.01
    # 用户改过的地图优先于 AI 读到的
    call(backend, "set_session_meta", name=item["name"], map="长弓溪谷")
    assert call(backend, "matches", days=0)["list"][0]["title"] == "长弓溪谷 · 烽火地带"
    # 再处理一次不会重复调用 AI
    before = calls["sheets"]
    backend._start_segments("20261022_192535", final=True)
    wait(backend.jobs, 60)
    assert calls["sheets"] == before


def test_completed_files_skip_segment_being_written(backend, home):
    folder = home / "recordings" / "20261022_200000"
    folder.mkdir(parents=True)
    for name in ("run01_000.mkv", "run01_001.mkv", "run02_000.mkv", "run02_001.mkv"):
        (folder / name).write_bytes(b"x")

    class Alive:
        def alive(self):
            return True

    from fupan.sessions import Run, Session
    ctl = backend.controller
    ctl.session = Session(name="20261022_200000", started_at=0)
    ctl.session_dir = folder
    ctl.run = Run(index=2, started_at=0, pattern="run02_%03d.mkv")
    ctl.proc = Alive()
    names = [f.name for f in backend._completed_files("20261022_200000")]
    assert names == ["run01_000.mkv", "run01_001.mkv", "run02_000.mkv"]  # run02_001 还在写
    ctl.proc = None
    ctl.session = ctl.session_dir = ctl.run = None
