"""改进建议：只根据你确认过死因的阵亡给出，一次最多两条，每条只改一项，采纳后自动复测。

规则（最近 30 天、最多 40 次已确认阵亡，至少 5 次才开始给建议）：
- 压枪散了 ≥30%：举枪瞄准垂直灵敏度 +0.05（上限 1.2，到上限就换更好压的改法）
- 没先看到人 ≥35%：耳机 + 听声走位
- 开火太晚 ≥30%：战术侧瞄独立按键
- 预瞄位置偏了 ≥30%：FOV 过大先调回 90~100，否则练爆头线
- 身位暴露 ≥30%：晃身 Peek
- 被多打少 ≥30%：交火纪律
- 装备劣势 ≥25%：打人用 5 级弹
复测：采纳后至少再打 3 局、5 次已确认阵亡，这个死因的占比下降 30% 以上算有效，否则提示改回。
设置项名称来自玩家视频里的游戏设置截图，游戏版本更新后可能变化。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from .stats import CAUSES, confirmed_cause, distance_profile, duel_summary

MIN_DEATHS = 5
MAX_ACTIVE = 2
WINDOW_DAYS = 30
WINDOW_MAX_DEATHS = 40
VERIFY_SESSIONS = 3
VERIFY_DEATHS = 5
KEEP_RATIO = 0.7
COOLDOWN_DAYS = 14


@dataclass
class Advice:
    rule: str
    cause: str
    title: str
    change_text: str
    how: str
    verify: str
    change: dict  # {"key": 设置项, "from": 原值, "to": 新值}，没有具体数值时为空


def _num(profile: dict, key: str):
    value = (profile or {}).get(key)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _verify(label: str) -> str:
    return f"再打 {VERIFY_SESSIONS} 局后对比“{label}”的占比，没有下降就提示改回。"


def advice_for(cause: str, profile: dict) -> Advice | None:
    label = CAUSES.get(cause, cause)
    if cause == "recoil":
        v = _num(profile, "ads_vertical")
        if v is not None and v >= 1.2 - 1e-9:
            return Advice("R_RECOIL", cause, "换一套更好压的改枪方案", f"垂直灵敏度已到 {v:.2f}，不再上调",
                          "灵敏度别再往上加。到“改枪码推荐”里选标着“压枪 / 稳定”的方案，配件优先堆后坐力和"
                          "额外开火时枪身稳定；中距离以上改成短点射。", _verify(label), {})
        if v is not None:
            new = round(min(1.2, v + 0.05), 2)
            return Advice("R_RECOIL", cause, "举枪瞄准垂直灵敏度上调 0.05", f"当前 {v:.2f} → 建议 {new:.2f}（+0.05）",
                          f"设置 → 键盘&鼠标 → 鼠标灵敏度 → 举枪瞄准垂直灵敏度改为 {new:.2f}（常用范围 0.9～1.2，"
                          "一般不超过 1.2）。改完去靶场 25 米压一梭子，子弹不往上飘就对了。",
                          _verify(label), {"key": "ads_vertical", "from": v, "to": new})
        return Advice("R_RECOIL", cause, "举枪瞄准垂直灵敏度上调 0.05", "+0.05（上限 1.2）",
                      "设置 → 键盘&鼠标 → 鼠标灵敏度 → 举枪瞄准垂直灵敏度在现在的基础上加 0.05，不超过 1.2。"
                      "先到本软件“设置 → 我的游戏灵敏度设置”填上当前数值，建议里就会写出具体数字。", _verify(label), {})
    if cause == "unseen":
        return Advice("R_UNSEEN", cause, "先听后动：用耳机和脚步声找人", "音频 + 转点习惯",
                      "戴耳机玩，在 设置 → 音频 里把输出设成耳机（有 3D / 双耳音效的选项就打开，不同版本名称可能不同）。"
                      "转点前停 1 秒听脚步；绕后时别跑跑跳跳；没有信息时不要站在空地上死架一个点。", _verify(label), {})
    if cause == "slow":
        return Advice("R_SLOW", cause, "近距离先侧瞄开火，不等完全开镜", "战术侧瞄独立按键",
                      "操控&战斗 → 战斗 → 瞄准设为「按住」；战术侧瞄独立控制选「独立按键」，再到 操控&战斗 → 战术侧瞄 "
                      "绑一个鼠标侧键。贴脸遭遇按侧键直接开火。改之前先截图记下原设置，去靶场试 10 分钟，不顺手就改回。",
                      _verify(label), {})
    if cause == "crosshair":
        fov = _num(profile, "fov")
        if fov is not None and fov > 100:
            return Advice("R_CROSSHAIR", cause, "FOV 调回 90～100", f"当前 {fov:.0f} → 建议 100",
                          "FOV 越大，敌人在屏幕上越小，越难对准。把游戏里的 FOV 调到 100（想更准可以 90），"
                          "一周内不要再改，让手感稳定下来。", _verify(label), {"key": "fov", "from": fov, "to": 100})
        return Advice("R_CROSSHAIR", cause, "准星常驻爆头线", "预瞄练习 · 每天 5 分钟",
                      "走路时把准星放在敌人头部可能出现的高度（门框、掩体边缘），露头时只需要微调；"
                      "每天开局前进靶场，15 米和 25 米定点打头各 3 组，大部分子弹在头和上胸算合格。", _verify(label), {})
    if cause == "exposed":
        return Advice("R_EXPOSED", cause, "用晃身 Peek 拿信息，别整个人出去", "DEA / AQD 晃身",
                      "站在掩体后往右看：D 露一点 → E 歪头看 → A 缩回；往左是 A → Q → D。拿到信息后不要反复从同一个"
                      "位置探头；对枪时 45° 斜向位移，不要直着突脸。", _verify(label), {})
    if cause == "outnumbered":
        return Advice("R_OUTNUM", cause, "避免一打多：先撤再逐个打", "交火纪律",
                      "听到两个以上方向的脚步或枪声，先退到掩体后等队友或拉开距离，让对面一个一个过来；"
                      "打掉一个人后马上换位置，不在原地打药、捡东西。", _verify(label), {})
    if cause == "gear":
        return Advice("R_GEAR", cause, "打人用 5 级弹", "子弹等级不低于对方护甲",
                      "端游打人至少带 5 级弹（社区共识），低级弹只用来清人机。低一级的弹打高一级的甲要多打好几枪，"
                      "对枪很吃亏。", _verify(label), {})
    return None


THRESHOLDS = {"recoil": 0.30, "unseen": 0.35, "slow": 0.30, "crosshair": 0.30, "exposed": 0.30,
              "outnumbered": 0.30, "gear": 0.25}


def _at(e: dict) -> float:
    """事件发生的时间：场次开始时间 + 录像内时间（后台加载事件时填 at），没有就用标注时间。"""
    return float(e.get("at") or e.get("created") or 0)


def recent_deaths(events: list[dict], now: float) -> list[dict]:
    cutoff = now - WINDOW_DAYS * 86400
    deaths = [e for e in events if confirmed_cause(e) and _at(e) >= cutoff]
    deaths.sort(key=_at, reverse=True)
    return deaths[:WINDOW_MAX_DEATHS]


def triggered(deaths: list[dict]) -> list[tuple[str, float, int, int]]:
    """[(死因, 占比, 次数, 总数)]，按占比从高到低。"""
    n = len(deaths)
    if n < MIN_DEATHS:
        return []
    counts: dict[str, int] = {}
    for e in deaths:
        counts[e["cause"]] = counts.get(e["cause"], 0) + 1
    out = [(c, k / n, k, n) for c, k in counts.items() if c in THRESHOLDS and k / n >= THRESHOLDS[c]]
    out.sort(key=lambda x: (-x[1], list(THRESHOLDS).index(x[0])))
    return out


def evidence_text(cause: str, share: float, k: int, n: int, deaths: list[dict]) -> str:
    text = f"最近 {n} 次已确认阵亡中 {k} 次（{share:.0%}）是“{CAUSES[cause]}”。"
    label, _code, dshare, dn = distance_profile([e for e in deaths if e.get("cause") == cause])
    if label:
        text += f"这些阵亡里 AI 读出的交战距离 {dshare:.0%} 在 {label}。"
    return text


def refresh(db, events: list[dict], profile: dict, now: float | None = None) -> None:
    """按最新标注更新“待应用”的建议。已采纳的建议不动，等复测。"""
    now = now or time.time()
    deaths = recent_deaths(events, now)
    hits = triggered(deaths)
    hit_causes = {c for c, *_ in hits}
    open_recs = db.query("SELECT * FROM recommendations WHERE status IN ('active','adopted') ORDER BY created")
    for rec in open_recs:
        cause = (json.loads(rec.get("baseline") or "{}")).get("cause", "")
        if rec["status"] == "active" and cause not in hit_causes:
            db.execute("DELETE FROM recommendations WHERE id=?", (rec["id"],))
    open_recs = db.query("SELECT * FROM recommendations WHERE status IN ('active','adopted') ORDER BY created")
    open_causes = {json.loads(r.get("baseline") or "{}").get("cause", "") for r in open_recs}
    cooldown = {json.loads(r.get("baseline") or "{}").get("cause", "")
                for r in db.query("SELECT baseline FROM recommendations WHERE status IN ('kept','reverted') AND closed_at>=?",
                                  (now - COOLDOWN_DAYS * 86400,))}
    for cause, share, k, n in hits:
        advice = advice_for(cause, profile)
        if advice is None:
            continue
        baseline = {"cause": cause, "share": share, "k": k, "n": n, "change": advice.change,
                    "duel": duel_summary(deaths)}
        fields = (advice.title, evidence_text(cause, share, k, n, deaths), advice.how, advice.change_text,
                  advice.verify, json.dumps(baseline, ensure_ascii=False))
        existing = next((r for r in open_recs if json.loads(r.get("baseline") or "{}").get("cause") == cause), None)
        if existing and existing["status"] == "active":
            db.execute("UPDATE recommendations SET title=?, evidence=?, how=?, change_text=?, verify=?, baseline=? "
                       "WHERE id=?", fields + (existing["id"],))
            continue
        if cause in open_causes or cause in cooldown or len(open_causes) >= MAX_ACTIVE:
            continue
        db.execute("INSERT INTO recommendations(rule,title,evidence,how,change_text,verify,status,created,baseline) "
                   "VALUES(?,?,?,?,?,?,'active',?,?)",
                   (advice.rule,) + fields[:5] + (now, fields[5]))
        open_causes.add(cause)


def adopt(db, rec_id: int, events: list[dict], now: float | None = None) -> None:
    now = now or time.time()
    rec = db.one("SELECT * FROM recommendations WHERE id=?", (rec_id,))
    if not rec or rec["status"] != "active":
        return
    baseline = json.loads(rec.get("baseline") or "{}")
    deaths = recent_deaths(events, now)
    baseline["duel_before"] = duel_summary([e for e in events if _at(e) < now])
    if deaths:
        k = sum(1 for e in deaths if e["cause"] == baseline.get("cause"))
        baseline.update({"share": k / len(deaths), "k": k, "n": len(deaths)})
    db.execute("UPDATE recommendations SET status='adopted', adopted_at=?, baseline=? WHERE id=?",
               (now, json.dumps(baseline, ensure_ascii=False), rec_id))


def _pct(x) -> str:
    return "—" if x is None else f"{x:.0%}"


def evaluate(db, events: list[dict], sessions_since, now: float | None = None) -> None:
    """检查已采纳的建议是否到了复测条件。sessions_since(ts) 返回 ts 之后开始的场次数。"""
    now = now or time.time()
    for rec in db.query("SELECT * FROM recommendations WHERE status='adopted'"):
        baseline = json.loads(rec.get("baseline") or "{}")
        cause = baseline.get("cause", "")
        after = [e for e in events if _at(e) >= float(rec["adopted_at"] or now)]
        deaths = [e for e in after if confirmed_cause(e)]
        if sessions_since(rec["adopted_at"]) < VERIFY_SESSIONS or len(deaths) < VERIFY_DEATHS:
            continue
        share_after = sum(1 for e in deaths if e["cause"] == cause) / len(deaths)
        share_before = float(baseline.get("share") or 0)
        kept = share_after <= share_before * KEEP_RATIO
        duel_b = (baseline.get("duel_before") or {}).get("rate")
        duel_a = duel_summary(after)["rate"]
        duel = f"；对枪胜率 {_pct(duel_b)} → {_pct(duel_a)}" if duel_a is not None and duel_b is not None else ""
        label = CAUSES.get(cause, cause)
        if kept:
            text = (f"“{rec['title']}”之后，“{label}”的占比从 {share_before:.0%} 降到 {share_after:.0%}"
                    f"（{len(deaths)} 次阵亡）{duel}，建议保留。")
        else:
            change = baseline.get("change") or {}
            back = f"，可以把它改回 {change['from']:.2f}" if change.get("from") is not None else "，可以改回原来的设置"
            text = (f"“{rec['title']}”之后，“{label}”的占比 {share_before:.0%} → {share_after:.0%}"
                    f"（{len(deaths)} 次阵亡）{duel}，没有明显改善{back}。")
        db.execute("UPDATE recommendations SET status=?, result=?, closed_at=? WHERE id=?",
                   ("kept" if kept else "reverted", text, now, rec["id"]))


def progress_text(rec: dict, events: list[dict], sessions_since) -> str:
    after = [e for e in events if _at(e) >= float(rec["adopted_at"] or 0)]
    deaths = sum(1 for e in after if confirmed_cause(e))
    s = min(VERIFY_SESSIONS, sessions_since(rec["adopted_at"]))
    return f"（第 {s}/{VERIFY_SESSIONS} 局 · 已确认阵亡 {min(deaths, VERIFY_DEATHS)}/{VERIFY_DEATHS}）"


def for_dashboard(db, events: list[dict], sessions_since) -> tuple[list[dict], list[dict]]:
    recs = []
    for r in db.query("SELECT * FROM recommendations WHERE status IN ('active','adopted') "
                      "ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, created"):
        item = {k: r[k] for k in ("id", "title", "change_text", "status", "evidence", "how", "verify")}
        if r["status"] == "adopted":
            item["progress"] = progress_text(r, events, sessions_since)
        recs.append(item)
    verified = [{"status": r["status"], "text": r["result"]} for r in
                db.query("SELECT status, result FROM recommendations WHERE status IN ('kept','reverted') "
                         "ORDER BY closed_at DESC LIMIT 3")]
    return recs, verified
