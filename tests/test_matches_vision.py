import httpx

from fupan import aiclient, matches


def frames(spec: str, step: float = 3.0, start: float = 0.0, **extra):
    """用一串字母描述画面状态：L 大厅 O 加载 M 对局 P 地图 D 倒地 X 阵亡 R 结算 ? 看不出。"""
    names = {"L": "lobby", "O": "loading", "M": "match", "P": "map", "D": "down", "X": "dead", "R": "result", "?": "other"}
    out = []
    for i, ch in enumerate(spec):
        f = {"t": start + i * step, "state": names[ch]}
        out.append(f)
    for i, info in extra.items():
        out[int(i[1:])].update(info)
    return out


def test_one_extraction_match_with_death_and_result():
    spec = "LLLL" + "OOOO" + "M" * 40 + "DDXXXX" + "XXXX" + "RRRR" + "LLL"
    fr = frames(spec, f4={"map": "零号大坝"}, f60={"result": "撤离失败"})
    found = matches.detect(fr, final=False)
    assert len(found) == 1
    m = found[0]
    assert m.closed and m.t_start == 8 * 3 and m.map == "零号大坝" and m.result == "failed" and m.mode == "extraction"
    assert m.deaths == [(48 * 3, "dead")]  # 先倒地后阵亡，算一次
    assert m.t_end == 62 * 3  # 结算之后回到大厅的那一刻


def test_two_matches_and_knockdown_revived():
    one = "M" * 35 + "DD" + "M" * 10 + "RR"
    spec = "LL" + one + "LLLLLL" + "OO" + "M" * 40 + "X" * 5 + "RR" + "L"
    found = matches.detect(frames(spec), final=False)
    assert len(found) == 2 and all(m.closed for m in found)
    assert found[0].deaths == [((2 + 35) * 3, "down")]  # 被击倒后又起来了
    assert len(found[1].deaths) == 1 and found[1].deaths[0][1] == "dead"


def test_open_match_while_recording_and_final_close():
    spec = "LL" + "M" * 50
    found = matches.detect(frames(spec), final=False)
    assert len(found) == 1 and not found[0].closed
    found = matches.detect(frames(spec), final=True)
    assert found[0].closed and found[0].t_end == 52 * 3


def test_flicker_is_not_a_match_and_long_gap_ends_match():
    assert matches.detect(frames("LLMLLLLLLLLLLLL"), final=True) == []  # 单张误判不算一局
    spec = "M" * 40 + "?" * 25 + "LL"
    found = matches.detect(frames(spec), final=False)
    assert len(found) == 1 and found[0].closed and found[0].t_end == 40 * 3  # 60 秒没有对局画面就算结束


def test_vote_prefers_longest_result_name():
    fr = frames("M" * 40 + "RRR" + "L", f40={"result": "失败"}, f41={"result": "撤离失败"}, f42={"result": "撤离失败"})
    assert matches.detect(fr)[0].result == "failed"


def test_pick_models_prefers_flash_for_vision():
    assert aiclient.pick_default_models(["deepseek-flash", "deepseek-v4-pro"]) == ("deepseek-v4-pro", "deepseek-flash")
    assert aiclient.pick_default_models(["deepseek-v4-pro"])[1] == ""


def _client(answer):
    seen = []

    def handler(request):
        import json
        body = json.loads(request.content)
        seen.append(body)
        text = answer(body)
        return httpx.Response(200, json={"model": body["model"], "choices": [{"message": {"content": text}}],
                                         "usage": {"prompt_tokens": 300, "completion_tokens": 3}})
    return aiclient.AIClient("sk-test-123456789012", transport=httpx.MockTransport(handler)), seen


def test_vision_self_test_detects_blind_model(monkeypatch):
    monkeypatch.setattr(aiclient.random, "sample", lambda pop, k: ["绿", "红"])
    ok_client, seen = _client(lambda body: "绿,红")
    ok, msg, _ = aiclient.vision_self_test(ok_client, "deepseek-flash")
    assert ok and "看图正常" in msg
    images = [c for c in seen[0]["messages"][0]["content"] if c["type"] == "image_url"]
    assert len(images) == 2 and images[0]["image_url"]["url"].startswith("data:image/png;base64,")
    blind, _ = _client(lambda body: "红,蓝")  # 看不到图的模型只能瞎猜
    ok, msg, _ = aiclient.vision_self_test(blind, "deepseek-v4-pro")
    assert not ok and "deepseek-flash" in msg
