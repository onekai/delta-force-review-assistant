import json

import httpx
import pytest

from fupan import analysis
from fupan.db import DB


def test_parse_json_tolerates_wrapping():
    assert analysis.parse_json('```json\n{"death": [2, 3]}\n```') == {"death": [2, 3]}
    assert analysis.parse_json('结果如下：{"first": 4} 以上') == {"first": 4}
    assert analysis.parse_json('{"a": {"b": 1}} trailing {"c": 2}') == {"a": {"b": 1}}
    assert analysis.parse_json("看不清") == {}


def test_group_times_merges_consecutive_hits():
    assert analysis.group_times([30, 33, 36, 120, 123]) == [30, 120]


def test_clean_diagnosis_validates_fields():
    d = analysis.clean_diagnosis({"cause": "RECOIL", "distance_m": "35", "confidence": 1.7, "headline": "x" * 50,
                                  "killer_weapon": None}, "m")
    assert d["cause"] == "recoil" and d["distance_m"] == 35 and d["confidence"] == 1.0
    assert len(d["headline"]) == 30 and d["killer_weapon"] == ""
    assert analysis.clean_diagnosis({"cause": "aimbot"}, "m")["cause"] == "other"


class FakeTimeline:
    def __init__(self, sheets):
        self._sheets = sheets
        self.duration = 600

    def scan_sheets(self, *a, **k):
        return self._sheets

    def grid(self, times, cols, tile_width=480, mask=True):
        return b"jpeg"

    def frame(self, t, width=960, mask=False):
        return b"jpeg"


def _session(db, replies, tmp_path):
    """用假的 DeepSeek 接口回放预先写好的回答，记录收到的请求。"""
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        text = replies(body, len(calls))
        return httpx.Response(200, json={"model": "vision", "choices": [{"message": {"content": text}}],
                                         "usage": {"prompt_tokens": 400, "completion_tokens": 20}})

    ai = analysis.AISession("sk-test-key-123456789", "https://api.deepseek.com", "vision", db, 1_000_000,
                            transport=httpx.MockTransport(handler))
    return ai, calls


def test_classify_sheet_validates_states(tmp_path):
    img = tmp_path / "s.jpg"
    img.write_bytes(b"A")
    db = DB(tmp_path / "t.db")
    answer = '```json\n{"states": ["lobby", "MATCH", "dead", "飞天", "result"], "map": "零号大坝", "result": "撤离失败"}\n```'
    ai, calls = _session(db, lambda body, n: answer, tmp_path)
    res = analysis.classify_sheet(ai, {"path": str(img), "times": [0, 3, 6, 9, 12, 15]})
    assert res["states"] == ["lobby", "match", "dead", "other", "result", "other"]  # 不认识的、缺的都记成 other
    assert res["map"] == "零号大坝" and res["result"] == "撤离失败"
    prompt = calls[0]["messages"][0]["content"][0]["text"]
    assert "1～6" in prompt and "dead" in prompt and "水印" in prompt


def test_refine_picks_first_death_tile(tmp_path):
    db = DB(tmp_path / "t.db")
    ai, calls = _session(db, lambda body, n: '{"first": 5}', tmp_path)
    assert analysis.refine(ai, FakeTimeline([]), 30.0) == 28.0  # 30 - 6 + (5 - 1)
    ai2, _ = _session(db, lambda body, n: '{"first": 0}', tmp_path)
    assert analysis.refine(ai2, FakeTimeline([]), 30.0) == 30.0  # 看不出来就用原来的时间


def test_budget_limit_stops_before_calling(tmp_path):
    db = DB(tmp_path / "t.db")
    db.add_usage("m", "x", 999_000, 900)
    ai, calls = _session(db, lambda body, n: "{}", tmp_path)
    with pytest.raises(analysis.BudgetExceeded):
        analysis.diagnose(ai, FakeTimeline([]), 100)
    assert calls == []


def test_diagnose_sends_two_images_and_parses(tmp_path):
    db = DB(tmp_path / "t.db")
    answer = json.dumps({"cause": "recoil", "headline": "连射上飘", "distance_m": 42, "confidence": 0.6,
                         "killer_weapon": "M4A1"}, ensure_ascii=False)
    ai, calls = _session(db, lambda body, n: answer, tmp_path)
    d = analysis.diagnose(ai, FakeTimeline([]), 100)
    assert d["cause"] == "recoil" and d["distance_m"] == 42 and d["killer_weapon"] == "M4A1"
    images = [c for c in calls[0]["messages"][0]["content"] if c["type"] == "image_url"]
    assert len(images) == 2


def test_cancel_stops_calls(tmp_path):
    img = tmp_path / "s.jpg"
    img.write_bytes(b"A")
    db = DB(tmp_path / "t.db")
    ai, calls = _session(db, lambda body, n: "{}", tmp_path)
    ai.cancelled = lambda: True
    with pytest.raises(analysis.Cancelled):
        analysis.classify_sheet(ai, {"path": str(img), "times": [0]})
    assert calls == []
