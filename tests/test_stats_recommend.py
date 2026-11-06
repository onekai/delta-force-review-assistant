import json
import time

from fupan import recommend, stats
from fupan.db import DB


def ev(kind="death", t=10.0, cause="", source="manual", confirmed=False, session="s1", at=None, **kw):
    e = {"kind": kind, "t": t, "cause": cause, "source": source, "confirmed": int(confirmed), "session": session,
         "created": time.time()}
    if at is not None:
        e["at"] = at
    e.update(kw)
    return e


# ---- 统计口径 ----
def test_ai_deaths_count_but_causes_need_confirmation():
    events = [ev(source="ai"), ev(source="ai", confirmed=True, cause="recoil"), ev()]
    assert sum(stats.counted_death(e) for e in events) == 3
    assert sum(stats.unconfirmed_ai(e) for e in events) == 1
    assert stats.cause_counts(events) == {"recoil": 1}


def test_duel_counts_death_as_loss_once():
    events = [
        ev("duel_win", t=5),
        ev("death", t=20, cause="recoil", confirmed=True),
        ev("duel_loss", t=21),  # 和上面的阵亡是同一次对枪，不重复算
        ev("death", t=60, cause="unseen", confirmed=True),  # 没看到人不算对枪
        ev("death", t=90),  # 手动标的阵亡算一次对枪输
        ev("death", t=120, source="ai", cause="slow"),  # AI 找到的阵亡也算
    ]
    assert stats.duel_counts(events) == (1, 3)
    s = stats.duel_summary(events)
    assert s["total"] == 4 and abs(s["rate"] - 1 / 4) < 1e-9


def test_duel_merge_is_per_session():
    events = [ev("death", t=20, session="a"), ev("duel_loss", t=20, session="b")]
    assert stats.duel_counts(events) == (0, 2)


def test_top_cause_and_distance_profile():
    events = [ev(cause="recoil", confirmed=True, distance_m=30) for _ in range(3)] + \
             [ev(cause="unseen", confirmed=True, distance_m=8), ev(cause="slow")]  # 最后一条没确认
    cause, share, k, n = stats.top_cause(events)
    assert (cause, k, n) == ("recoil", 3, 4) and abs(share - 0.75) < 1e-9
    label, code, dshare, dn = stats.distance_profile(events)
    assert code == "mid" and dn == 4 and label == "15~50 米"  # 没有距离的那条不计入


# ---- 建议规则 ----
def deaths(causes, at):
    return [ev(cause=c, confirmed=True, at=at + i) for i, c in enumerate(causes)]


def test_no_advice_below_minimum(tmp_path):
    db = DB(tmp_path / "t.db")
    recommend.refresh(db, deaths(["recoil"] * 4, time.time() - 100), {})
    assert db.query("SELECT * FROM recommendations") == []


def test_recoil_advice_uses_profile_value_and_caps(tmp_path):
    db = DB(tmp_path / "t.db")
    events = deaths(["recoil"] * 4 + ["unseen", "slow"], time.time() - 100)
    recommend.refresh(db, events, {"ads_vertical": 1.05})
    recs, _ = recommend.for_dashboard(db, events, lambda ts: 0)
    assert len(recs) == 1
    assert recs[0]["change_text"] == "当前 1.05 → 建议 1.10（+0.05）"
    assert "67%" in recs[0]["evidence"]
    at_cap = recommend.advice_for("recoil", {"ads_vertical": 1.2})
    assert at_cap.change == {} and "不再上调" in at_cap.change_text
    assert recommend.advice_for("recoil", {"ads_vertical": 1.18}).change["to"] == 1.2


def test_at_most_two_open_recommendations(tmp_path):
    db = DB(tmp_path / "t.db")
    events = deaths(["recoil"] * 4 + ["slow"] * 4 + ["exposed"] * 4, time.time() - 100)
    recommend.refresh(db, events, {})
    rows = db.query("SELECT * FROM recommendations")
    assert len(rows) == 2


def test_active_advice_removed_when_no_longer_triggered(tmp_path):
    db = DB(tmp_path / "t.db")
    now = time.time()
    recommend.refresh(db, deaths(["recoil"] * 3 + ["slow", "unseen"], now - 100), {})
    assert len(db.query("SELECT * FROM recommendations")) == 1
    recommend.refresh(db, deaths(["recoil"] * 3 + ["slow"] * 8, now - 100), {})  # 压枪占比降到 27%
    rows = db.query("SELECT * FROM recommendations")
    assert [json.loads(r["baseline"])["cause"] for r in rows] == ["slow"]


def test_verification_keeps_or_reverts(tmp_path):
    db = DB(tmp_path / "t.db")
    t0 = time.time() - 10 * 86400
    before = deaths(["recoil"] * 4 + ["unseen"] * 2, t0)
    recommend.refresh(db, before, {"ads_vertical": 1.05}, now=t0 + 100)
    rec = db.one("SELECT * FROM recommendations")
    recommend.adopt(db, rec["id"], before, now=t0 + 200)
    # 复测样本还不够：不结案
    few = before + deaths(["unseen"] * 2, t0 + 300)
    recommend.evaluate(db, few, lambda ts: 3, now=t0 + 400)
    assert db.one("SELECT status FROM recommendations")["status"] == "adopted"
    # 采纳后 5 次阵亡里只有 1 次压枪问题：67% → 20%，有效
    good = before + deaths(["unseen", "unseen", "recoil", "slow", "unseen"], t0 + 300)
    recommend.evaluate(db, good, lambda ts: 3, now=t0 + 400)
    row = db.one("SELECT status, result FROM recommendations")
    assert row["status"] == "kept" and "67% 降到 20%" in row["result"]


def test_verification_reverts_with_original_value(tmp_path):
    db = DB(tmp_path / "t.db")
    t0 = time.time() - 10 * 86400
    before = deaths(["recoil"] * 4 + ["unseen"] * 2, t0)
    recommend.refresh(db, before, {"ads_vertical": 1.05}, now=t0 + 100)
    rec = db.one("SELECT * FROM recommendations")
    recommend.adopt(db, rec["id"], before, now=t0 + 200)
    bad = before + deaths(["recoil"] * 4 + ["unseen"], t0 + 300)
    recommend.evaluate(db, bad, lambda ts: 4, now=t0 + 400)
    row = db.one("SELECT status, result FROM recommendations")
    assert row["status"] == "reverted" and "改回 1.05" in row["result"]
