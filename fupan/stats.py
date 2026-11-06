"""标注数据的统计口径。界面、建议规则和改枪码匹配都用这里的定义，保证数字一致。

- 阵亡：手动标记的和 AI 找到的都算（AI 找错了可以删掉）。AI 给的死因没确认的，单独计数提醒你确认。
- 死因统计和改进建议：只用你确认过死因的阵亡。
- 对枪胜率 = 对枪赢 ÷（对枪赢 + 对枪输）。
  每次阵亡（死因不是“没先看到人”）自动算一次对枪输；如果 3 秒内已经手动标了“对枪输”，不重复算。
"""

from __future__ import annotations

CAUSES = {
    "unseen": "没先看到人", "crosshair": "预瞄位置偏了", "slow": "开火太晚", "recoil": "压枪散了",
    "exposed": "身位暴露", "outnumbered": "被多打少", "gear": "装备劣势", "other": "其他",
}
NOT_A_DUEL = {"unseen"}
DUEL_MERGE_SEC = 3.0


def counted_death(ev: dict) -> bool:
    return ev.get("kind") == "death"


def unconfirmed_ai(ev: dict) -> bool:
    return ev.get("kind") == "death" and ev.get("source") == "ai" and not ev.get("confirmed")


def confirmed_cause(ev: dict) -> str:
    if ev.get("kind") == "death" and ev.get("confirmed") and ev.get("cause") in CAUSES:
        return ev["cause"]
    return ""


def duel_counts(events: list[dict]) -> tuple[int, int]:
    """events 可以混合多个场次。返回 (赢, 输)。"""
    wins = sum(1 for e in events if e.get("kind") == "duel_win")
    losses_marked = [e for e in events if e.get("kind") == "duel_loss"]
    losses = len(losses_marked)
    for e in events:
        if not counted_death(e) or e.get("cause") in NOT_A_DUEL:
            continue
        dup = any(m.get("session") == e.get("session") and abs(float(m["t"]) - float(e["t"])) <= DUEL_MERGE_SEC
                  for m in losses_marked)
        if not dup:
            losses += 1
    return wins, losses


def duel_summary(events: list[dict]) -> dict:
    w, l = duel_counts(events)
    total = w + l
    return {"wins": w, "losses": l, "total": total, "rate": (w / total) if total else None}


def cause_counts(events: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in events:
        c = confirmed_cause(e)
        if c:
            counts[c] = counts.get(c, 0) + 1
    return counts


def top_cause(events: list[dict]) -> tuple[str, float, int, int]:
    """(最多的死因, 占比, 次数, 已确认死因的阵亡总数)。没有数据时死因为空。"""
    counts = cause_counts(events)
    n = sum(counts.values())
    if not n:
        return "", 0.0, 0, 0
    cause, k = max(counts.items(), key=lambda kv: (kv[1], kv[0] != "other"))
    return cause, k / n, k, n


DISTANCE_BUCKETS = [(0, 15, "15 米以内", "close"), (15, 50, "15~50 米", "mid"), (50, 10_000, "50 米以上", "long")]


def distance_profile(events: list[dict]) -> tuple[str, str, float, int]:
    """AI 读出的交战距离分布：(区间名, 区间代号, 占比, 样本数)。"""
    dists = [float(e["distance_m"]) for e in events
             if e.get("kind") == "death" and e.get("distance_m") not in (None, "") and float(e["distance_m"]) > 0]
    if len(dists) < 3:
        return "", "", 0.0, len(dists)
    best = max(DISTANCE_BUCKETS, key=lambda b: sum(1 for d in dists if b[0] <= d < b[1]))
    share = sum(1 for d in dists if best[0] <= d < best[1]) / len(dists)
    return best[2], best[3], share, len(dists)
