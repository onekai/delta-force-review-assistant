"""本机文字识别切局、OCR 与 AI 的合并规则、本局方案和 AI 教练点评。"""

import json
import sys
import time

import httpx
import pytest

from fupan import media, ocr
from fupan.aiclient import AIClient
from fupan.backend import _merge_state
from fupan.encoders import find_ffmpeg

needs_ffmpeg = pytest.mark.skipif(not find_ffmpeg(), reason="没有 FFmpeg")


# ---- 文字 → 画面状态 ----
def test_classify_lobby_needs_two_menu_words():
    assert ocr.classify("开始游戏  仓库  特战干员  部门")[0] == "lobby"
    assert ocr.classify("仓库")[0] == ""  # 只出现一个词不算（对局里也可能看到“仓库”）
    assert ocr.classify("开 始 游 戏\n交 易 行")[0] == "lobby"  # 识别结果字间带空格


def test_classify_result_dead_down_and_info():
    st, info = ocr.classify("撤离失败  带出价值 12.5万  零号大坝  烽火地带")
    assert st == "result" and info == {"map": "零号大坝", "mode": "烽火地带", "result": "撤离失败", "kw": "撤离失败"}
    assert ocr.classify("开始游戏 仓库")[1] == {"kw": "开始游戏+仓库"}
    assert ocr.classify("本局收益 3.2万")[0] == "result"
    assert ocr.classify("撤离成功")[1]["result"] == "撤离成功"
    assert ocr.classify("你已阵亡 正在观战 队友")[0] == "dead"
    assert ocr.classify("被击倒 等待队友救援")[0] == "down"
    assert ocr.classify("航天基地")[0] == "" and ocr.classify("航天基地")[1] == {"map": "航天基地"}
    assert ocr.classify("") == ("", {})


@pytest.mark.real_ocr
@pytest.mark.skipif(sys.platform == "win32", reason="Windows 上真的可用")
def test_ocr_unavailable_off_windows():
    assert ocr.available() is False and "Windows" in ocr.status()
    assert ocr.recognize(b"\xff\xd8 fake jpeg") == ""


# ---- 文字识别和 AI 的结果合并 ----
def test_merge_state_rules():
    # 大厅、结算有固定文字：以文字识别为准
    assert _merge_state(("lobby", {}), ("match", {}))[0] == "lobby"
    st, info = _merge_state(("result", {"result": "撤离成功"}), ("dead", {"map": "零号大坝"}))
    assert st == "result" and info == {"map": "零号大坝", "result": "撤离成功", "src": "mix"}
    # 其余以 AI 为准
    assert _merge_state(("", {}), ("dead", {})) == ("dead", {"src": "mix"})
    assert _merge_state(("down", {}), ("match", {}))[0] == "match"
    # AI 看不出时用文字识别的；文字识别也没认出特定文字，就是对局中
    assert _merge_state(("down", {}), ("other", {}))[0] == "down"
    assert _merge_state(("", {}), ("other", {}))[0] == "match"
    assert _merge_state(("", {"map": "巴克什"}), None) == ("match", {"map": "巴克什", "src": "ocr"})
    # 只有 AI
    assert _merge_state(None, ("loading", {})) == ("loading", {"src": "ai"})
    assert _merge_state(None, ("other", {}))[0] == "other"


# ---- 只有文字识别（没开 AI）：照样切局、找阵亡、出片段，点一下死因就出本局方案 ----
def fake_screen_text(t: float) -> str:
    if t < 9:
        return "腾讯游戏 健康游戏忠告"  # 启动画面：第一次看到大厅之前都不算对局
    if t < 30:
        return "开始游戏 仓库 特战干员 部门 交易行"
    if t < 39:
        return "零号大坝 烽火地带 部署中"
    if 120 <= t < 126:
        return "被击倒 等待救援"
    if 126 <= t < 150:
        return "你已阵亡 正在观战"
    if t < 210:
        return "M4A1 30/120"
    if t < 222:
        return "撤离失败 带出价值"
    return "开始游戏 仓库 特战干员"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUPAN_HOME", str(tmp_path))
    return tmp_path


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


def run_jobs(b, timeout=120):
    end = time.time() + timeout
    time.sleep(0.2)
    while b.jobs.active() and time.time() < end:
        time.sleep(0.2)
    assert not b.jobs.active(), b.jobs.list()


@needs_ffmpeg
def test_ocr_only_pipeline_and_match_advice(backend, home, monkeypatch):
    from test_media_backend import make_recording
    folder = home / "recordings" / "20261023_200000"
    make_recording(folder, 1, 240, size="320x180", seg=60)
    (folder / "session.json").write_text(json.dumps({"name": "20261023_200000", "started_at": 1792756800, "runs": [
        {"index": 1, "started_at": 1792756810, "encoder": "h264_amf", "crop": {"width": 320, "height": 180},
         "pattern": "run01_%03d.mkv"}]}), encoding="utf-8")
    real_frames_for = media.Timeline.frames_for
    seen_sizes = []

    def frames_for(self, seg, every=3.0, width=1280):  # 真的截帧，再把图片换成“时间”，方便假的识别按时间返回文字
        out = real_frames_for(self, seg, every, width)
        seen_sizes.extend(len(img) for _t, img in out)
        return [(t, str(t).encode()) for t, _img in out]

    monkeypatch.setattr(media.Timeline, "frames_for", frames_for)
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "recognize", lambda data: fake_screen_text(float(data.decode())))
    assert backend.ai_ready()[0] is False  # 没填密钥、没开 AI

    backend._start_segments("20261023_200000", final=True)
    run_jobs(backend)
    assert len(seen_sizes) == 80 and min(seen_sizes) > 500  # 240 秒每 3 秒一帧，都是真图片

    rows = backend.db.matches("20261023_200000")
    assert len(rows) == 1 and rows[0]["analyzed_at"] and rows[0]["result"] == "failed"
    assert rows[0]["t_start"] == 30 and abs(rows[0]["t_end"] - 222) < 4  # 启动画面（0~9 秒）没有被当成对局
    item = call(backend, "matches", days=0)["list"][0]
    assert item["title"] == "零号大坝 · 烽火地带" and item["death_count"] == 1
    d = call(backend, "session_detail", name=item["name"])
    ev = d["events"][0]
    assert ev["source"] == "auto" and ev["cause"] == "" and ev["clip"] and abs(ev["t"] - 90) < 0.1
    ma = d["match_advice"]
    assert ma["items"] == [] and ma["deaths"] == 1 and ma["uncaused"] == 1 and ma["coach"] == ""

    # 点一下死因 → 这一局马上有方案（不用等累计 5 次）
    call(backend, "update_event", id=ev["id"], cause="recoil", confirmed=True)
    ma = call(backend, "session_detail", name=item["name"])["match_advice"]
    assert len(ma["items"]) == 1 and ma["items"][0]["status"] == "match"
    assert "举枪瞄准垂直灵敏度" in ma["items"][0]["title"] and "已确认" in ma["items"][0]["evidence"]
    dash = call(backend, "dashboard")
    assert dash["match_advice"]["items"][0]["title"] == ma["items"][0]["title"]
    assert dash["recommendations"] == []  # 长期方案仍然要累计 5 次

    # 把这一局保存成一个完整的录像文件：不重新编码，时长和这一局一致，不算新的场次，也不会被自动清理
    out = call(backend, "export_match", name=item["name"])
    run_jobs(backend)
    saved = list((backend.cfg.recordings_path() / "单局录像").glob("*.mkv"))
    assert len(saved) == 1 and saved[0].name.endswith("零号大坝_烽火地带.mkv") and out["folder"] == str(saved[0].parent)
    assert abs(media.probe(saved[0])["duration"] - 192) < 3
    assert [s["name"] for s in call(backend, "matches", days=0)["list"]] == [item["name"]]
    from fupan import sessions
    assert sessions.cleanup(backend.cfg.recordings_path(), 1, now=time.time() + 10 * 86400) and saved[0].exists()


def test_match_advice_marks_ai_causes_and_coach(backend, monkeypatch, memory_keyring):
    session = "20261023_210000"
    cur = backend.db.execute("INSERT INTO matches(session,t_start,t_end,closed,map,mode,result,deaths_json,created) "
                             "VALUES(?,?,?,?,?,?,?,?,?)", (session, 100, 700, 1, "长弓溪谷", "extraction", "failed",
                                                           "[]", time.time()))
    match_id = int(cur.lastrowid)
    e1 = backend.db.add_event(session, 300, "death", source="ai")
    backend.db.update_event(e1, cause="unseen", ai_json=json.dumps({"process": "侧面被打", "advice": "转点前先听"}))
    e2 = backend.db.add_event(session, 650, "death", source="ai")
    backend.db.update_event(e2, cause="unseen", confirmed=1)
    backend.db.add_event(session, 900, "death", source="manual")  # 不在这一局里

    v = {"match_id": match_id, "session": session, "name": f"{session}#{match_id}", "title": "长弓溪谷 · 烽火地带"}
    events = [e for e in backend.db.events(session) if 100 <= e["t"] <= 700]
    ma = backend._match_advice(v, events)
    assert ma["deaths"] == 2 and len(ma["items"]) == 1
    assert "其中 1 次已确认" in ma["items"][0]["evidence"] and ma["items"][0]["status"] == "match"

    # AI 教练点评：只发这一局的文字数据，关掉思考模式，结果存进数据库
    seen = {}

    def handler(request):
        body = json.loads(request.content)
        seen.update(body)
        return httpx.Response(200, json={"model": body["model"], "usage": {"prompt_tokens": 300, "completion_tokens": 80},
                                         "choices": [{"message": {"content": "- 转点前停一秒听脚步\n- 别在空地架点\n- 先找掩体再打药\n多余的一行"}}]})

    memory_keyring["deepseek"] = "sk-test-abcdefghijkl"
    backend._key = None
    backend.cfg.ai_vision_enabled = True
    backend.cfg.ai_text_model = "deepseek-v4-pro"
    monkeypatch.setattr(backend, "_client", lambda timeout=60: AIClient(
        "sk-test-abcdefghijkl", transport=httpx.MockTransport(handler)))
    coach = backend._coach(match_id)
    assert coach.splitlines() == ["转点前停一秒听脚步", "别在空地架点", "先找掩体再打药"]
    prompt = seen["messages"][0]["content"]
    assert seen["model"] == "deepseek-v4-pro" and seen["thinking"] == {"type": "disabled"}
    assert "长弓溪谷" in prompt and "撤离失败" in prompt and "阵亡 2 次" in prompt and "没先看到人" in prompt
    assert "侧面被打" in prompt and "15:00" not in prompt  # 局外的阵亡不算
    assert backend.db.usage_today() == 380

    # 额度不够时不写点评
    backend.cfg.ai_daily_token_limit = 1000
    assert backend._coach(match_id) == ""
