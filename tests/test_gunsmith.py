import time

import httpx
import pytest

from fupan import gunsmith
from fupan.db import DB


def test_parse_full_codes_only():
    text = ("1.SR-3M紧凑突击步枪-烽火地带-6H5RLPK0A2GV48FMPKM4H（24万 近战跑打）\n"
            "改装方案KC17突击步枪-烽火地带-6JOO50K0AEK0SD1PJ432L 18W 压枪稳\n"
            "【AS Val突击步枪-全面战场-6H5RLPK0A2GV48FMPKMXX】\n"
            "汤姆逊冲锋枪-烽火地带-6L6LBT009TGG7C6MIGQID\n"
            "无敌版（18W）:-6JOO50K0AEK0SD1PJ432L")
    codes = {c["weapon"]: c for c in gunsmith.parse_codes(text, "")}
    assert set(codes) == {"SR-3M紧凑突击步枪", "KC17突击步枪", "AS Val突击步枪", "汤姆逊冲锋枪"}
    assert codes["KC17突击步枪"]["code"] == "KC17突击步枪-烽火地带-6JOO50K0AEK0SD1PJ432L"
    assert codes["KC17突击步枪"]["budget_wan"] == 18 and "压枪" in codes["KC17突击步枪"]["tags"]
    assert codes["SR-3M紧凑突击步枪"]["budget_wan"] == 24 and codes["SR-3M紧凑突击步枪"]["uses"] == ["近距离"]
    assert codes["AS Val突击步枪"]["mode"] == "全面战场"


def test_code_must_end_at_boundary():
    assert gunsmith.parse_codes("M4A1突击步枪-烽火地带-6H5RLPK0A2GV48FMPKM4Habc") == []


def _store_row(db, code, weapon, views, favorites, days_old, uses="", tags="", budget=None, invalid=0):
    now = time.time()
    db.execute("INSERT INTO codes(code,weapon,mode,title,author,bvid,url,cover,pubdate,views,favorites,invalid,sources,"
               "first_seen,last_seen,uses,budget_wan) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (code, weapon, "烽火地带", "t", "a", "BV1", f"https://www.bilibili.com/video/{code[-4:]}", "",
                now - days_old * 86400, views, favorites, invalid, 1, now, now, f"{uses}#{tags}", budget))


def test_ranking_prefers_fresh_popular_and_matching(tmp_path):
    db = DB(tmp_path / "t.db")
    _store_row(db, "A-烽火地带-AAAAAAAAAAAAAAAAAAAA1", "A", 500000, 10000, 3)
    _store_row(db, "B-烽火地带-AAAAAAAAAAAAAAAAAAAA2", "B", 500000, 10000, 90)
    _store_row(db, "C-烽火地带-AAAAAAAAAAAAAAAAAAAA3", "C", 500000, 10000, 3, uses="中距离", tags="压枪")
    _store_row(db, "D-烽火地带-AAAAAAAAAAAAAAAAAAAA4", "D", 500000, 10000, 400)  # 太旧，不收录
    _store_row(db, "E-烽火地带-AAAAAAAAAAAAAAAAAAAA5", "E", 500000, 10000, 3, invalid=3)
    plain = [c["weapon"] for c in gunsmith.ranked(db)]
    assert "D" not in plain and plain.index("A") < plain.index("B") and plain.index("A") < plain.index("E")
    matched = gunsmith.ranked(db, issue="recoil", distance="mid")
    assert matched[0]["weapon"] == "C" and matched[0]["matched"] and "压枪" in matched[0]["match_reason"]


def test_budget_filter_keeps_unknown_prices(tmp_path):
    db = DB(tmp_path / "t.db")
    _store_row(db, "A-烽火地带-AAAAAAAAAAAAAAAAAAAA1", "A", 1000, 10, 3, budget=40)
    _store_row(db, "B-烽火地带-AAAAAAAAAAAAAAAAAAAA2", "B", 1000, 10, 3, budget=12)
    _store_row(db, "C-烽火地带-AAAAAAAAAAAAAAAAAAAA3", "C", 1000, 10, 3)
    assert {c["weapon"] for c in gunsmith.ranked(db, budget="低于15万")} == {"B", "C"}


def test_failed_feedback_pushes_code_down(tmp_path):
    db = DB(tmp_path / "t.db")
    _store_row(db, "A-烽火地带-AAAAAAAAAAAAAAAAAAAA1", "A", 500000, 10000, 3)
    _store_row(db, "B-烽火地带-AAAAAAAAAAAAAAAAAAAA2", "B", 400000, 9000, 3)
    for _ in range(2):
        db.execute("INSERT INTO code_feedback(code,ok,ts) VALUES(?,0,?)", ("A-烽火地带-AAAAAAAAAAAAAAAAAAAA1", time.time()))
    assert gunsmith.ranked(db)[0]["weapon"] == "B"


def _item(bvid, desc, views=1000, aid=1):
    return {"bvid": bvid, "aid": aid, "title": "三角洲行动 改枪", "desc": desc, "pic": "http://i0.hdslb.com/x.jpg",
            "pubdate": time.time() - 86400, "owner": {"name": "up"}, "stat": {"view": views, "favorite": 10}}


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.bilibili.com")


def test_crawl_collects_codes_and_skips_blocked_comments():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path.endswith("/view"):
            return httpx.Response(200, json={"code": 0, "data": _item("BV1AAAAAAAAA", "KC17突击步枪-烽火地带-6JKOR4O05RNT3L18KEABF")})
        if request.url.path.endswith("/related"):
            return httpx.Response(200, json={"code": 0, "data": [
                _item("BV1BBBBBBBBB", "M7战斗步枪-烽火地带-6LIL8N001QPUJ3FQQ375A 压枪", 5000, 2),
                _item("BV1bad", "M7战斗步枪-烽火地带-6LIL8N001QPUJ3FQQ375B")]})  # 编号格式不对，丢弃
        return httpx.Response(200, json={"code": -352, "message": "-352"})

    crawler = gunsmith.Crawler(client=_client(handler), gap=0)
    videos, note = gunsmith.crawl(["BV1AAAAAAAAA"], crawler)
    assert note == "" and crawler.comments_blocked
    assert set(videos) == {"BV1AAAAAAAAA", "BV1BBBBBBBBB"}
    assert sum(p.endswith("/reply") for p in seen) == 1  # 评论被限制后不再请求


def test_crawl_stops_on_captcha():
    def handler(request):
        if request.url.path.endswith("/view"):
            return httpx.Response(200, json={"code": 0, "data": _item("BV1AAAAAAAAA", "KC17突击步枪-烽火地带-6JKOR4O05RNT3L18KEABF")})
        return httpx.Response(200, json={"code": -352, "data": {"v_voucher": "voucher_x"}})

    videos, note = gunsmith.crawl(["BV1AAAAAAAAA", "BV1CCCCCCCCC"], gunsmith.Crawler(client=_client(handler), gap=0))
    assert "限制" in note and list(videos) == ["BV1AAAAAAAAA"]


def test_store_dedupes_by_code(tmp_path):
    db = DB(tmp_path / "t.db")
    a = gunsmith.Video("BV1AAAAAAAAA", 1, "t", "", "up", "", time.time(), 100, 1,
                       gunsmith.parse_codes("KC17突击步枪-烽火地带-6JKOR4O05RNT3L18KEABF"))
    b = gunsmith.Video("BV1BBBBBBBBB", 2, "t2", "", "up2", "", time.time(), 900, 1,
                       gunsmith.parse_codes("KC17突击步枪-烽火地带-6JKOR4O05RNT3L18KEABF"))
    n, nv = gunsmith.store(db, {"a": a, "b": b})
    row = db.one("SELECT * FROM codes")
    assert (n, nv) == (1, 2) and row["sources"] == 2 and row["bvid"] == "BV1BBBBBBBBB"


@pytest.mark.parametrize("pic,expected", [
    ("http://i0.hdslb.com/bfs/a.jpg", "https://i0.hdslb.com/bfs/a.jpg@480w_270h_1c.jpg"), ("", "")])
def test_cover_url(pic, expected):
    assert gunsmith._cover(pic) == expected


def test_price_from_neighbor_lines_and_title():
    above = ("【方案一】平民 12w\n"
             "AK-12突击步枪-烽火地带-6H5RLPK0A2GV48FMPKMA1\n"
             "【方案二】满改 45万\n"
             "M4A1突击步枪-烽火地带-6H5RLPK0A2GV48FMPKMA2\n"
             "M7战斗步枪-烽火地带-6H5RLPK0A2GV48FMPKMA3")
    got = {c["weapon"]: c["budget_wan"] for c in gunsmith.parse_codes(above, "三把枪 10万播放感谢")}
    assert got == {"AK-12突击步枪": 12, "M4A1突击步枪": 45, "M7战斗步枪": None}  # “10万播放”不是价格
    below = ("AK-12突击步枪-烽火地带-6H5RLPK0A2GV48FMPKMB1\n价格：12.5W\n"
             "M4A1突击步枪-烽火地带-6H5RLPK0A2GV48FMPKMB2\n造价 380000")
    got = {c["weapon"]: c["budget_wan"] for c in gunsmith.parse_codes(below, "")}
    assert got == {"AK-12突击步枪": 12.5, "M4A1突击步枪": 38}
    # 码里面的“12W”不是价格；标题里只有一个价格时用标题的
    one = gunsmith.parse_codes("Vector冲锋枪-烽火地带-6H5RLPK0A2G12W8FMPKM", "【三角洲】Vector 只要 18万")
    assert one[0]["budget_wan"] == 18
    two = gunsmith.parse_codes("Vector冲锋枪-烽火地带-6H5RLPK0A2G12W8FMPKM", "15万和60万两套")
    assert two[0]["budget_wan"] is None


def test_price_buckets():
    assert [gunsmith.bucket_of(x) for x in (None, 3, 14.9, 15, 29.9, 30, 59, 60, 180)] == [
        "价格未标注", "低于15万", "低于15万", "15万~30万", "15万~30万", "30万~60万", "30万~60万", "60万以上", "60万以上"]
    assert gunsmith.price_label(28.5) == "约 28.5 万" and gunsmith.price_label(None) == ""
