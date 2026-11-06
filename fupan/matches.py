"""按局切分：根据 AI 对每隔几秒一张截图判断出的画面状态，找出每一局的开始、结束和局内的阵亡。

画面状态（analysis.STATES）：
  lobby 大厅菜单 · loading 匹配/加载/部署 · match 对局中 · map 对局中开地图/背包
  down 自己被击倒 · dead 自己阵亡/观战 · result 结算 · other 看不出

规则：
- 一局从“对局中”类画面开始（15 秒内至少 3 张对局画面，避免单张误判）。
- 出现结算画面后回到大厅，这一局结束；或者“对局外”画面持续超过 60 秒，也算结束。
- 游戏关闭（final=True）时，还没结束的一局在最后一张画面处结束。
- 不到 90 秒的“局”丢掉（多半是误判）。
- 局内连续的“被击倒/阵亡”画面算一次阵亡，记第一张的时间；只有被击倒、之后又回到对局画面的，记为“被击倒”。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

IN_MATCH = {"match", "map", "down", "dead"}
DEATH_STATES = {"down", "dead"}
START_WINDOW = 15.0
START_MIN_FRAMES = 3
END_GAP = 60.0
MIN_MATCH = 90.0
DEATH_MERGE = 9.0
MAP_LOOKBACK = 120.0

MAPS = ("零号大坝", "长弓溪谷", "航天基地", "巴克什", "潮汐监狱")
MODES = {"烽火地带": "extraction", "全面战场": "warfare"}
RESULTS = {"撤离成功": "extracted", "撤离失败": "failed", "胜利": "win", "失败": "loss"}


@dataclass
class Match:
    t_start: float
    t_end: float
    closed: bool
    deaths: list = field(default_factory=list)  # [(时间, "dead" 或 "down")]
    map: str = ""
    mode: str = ""
    result: str = ""


def _confirmed_start(frames: list[dict], i: int) -> bool:
    t = frames[i]["t"]
    n = sum(1 for f in frames[i:] if f["t"] - t <= START_WINDOW and f["state"] in IN_MATCH)
    return n >= START_MIN_FRAMES


def detect(frames: list[dict], step: float = 3.0, final: bool = False) -> list[Match]:
    """frames: [{"t": 整场时间, "state": 状态, "map"/"mode"/"result": AI 读到的文字（可空）}]，按时间排好。"""
    out: list[Match] = []
    cur = None  # {"start", "last_in", "last_result"}
    for i, f in enumerate(frames):
        t, st = float(f["t"]), f["state"]
        if cur is None:
            if st in IN_MATCH and _confirmed_start(frames, i):
                cur = {"start": t, "last_in": t, "last_result": None}
            continue
        if st in IN_MATCH:
            cur["last_in"] = t
            continue
        if st == "result":
            cur["last_result"] = t
            continue
        # 对局外的画面（大厅、加载、看不出）
        if cur["last_result"] is not None and st == "lobby":
            out.append(_close(frames, cur, t))
            cur = None
        elif t - max(cur["last_in"], cur["last_result"] or 0) > END_GAP:
            out.append(_close(frames, cur, max(cur["last_in"], cur["last_result"] or 0) + step))
            cur = None
    if cur is not None:
        if final and frames:
            out.append(_close(frames, cur, float(frames[-1]["t"]) + step))
        else:
            end = max(cur["last_in"], cur["last_result"] or 0) + step
            m = _close(frames, cur, end)
            m.closed = False
            out.append(m)
    return [m for m in out if m.t_end - m.t_start >= MIN_MATCH or not m.closed]


def _close(frames: list[dict], cur: dict, t_end: float) -> Match:
    m = Match(t_start=cur["start"], t_end=t_end, closed=True)
    inside = [f for f in frames if m.t_start <= float(f["t"]) < m.t_end]
    m.deaths = death_groups(inside)
    before = [f for f in frames if m.t_start - MAP_LOOKBACK <= float(f["t"]) < m.t_end]
    m.map = _vote(before, "map", MAPS)
    m.mode = MODES.get(_vote(before, "mode", tuple(MODES)), "")
    results = [f for f in inside if f["state"] == "result"] or inside
    m.result = RESULTS.get(_vote(results, "result", tuple(RESULTS)), "")
    if not m.mode and m.result:  # 撤离只有烽火地带有，胜负只有全面战场有
        m.mode = "extraction" if m.result in ("extracted", "failed") else "warfare"
    return m


def death_groups(frames: list[dict]) -> list[tuple[float, str]]:
    """连续的“被击倒/阵亡”画面合成一次：(第一张的时间, "dead" 或 "down")。"""
    groups: list[list[dict]] = []
    for f in frames:
        if f["state"] not in DEATH_STATES:
            continue
        if groups and float(f["t"]) - float(groups[-1][-1]["t"]) <= DEATH_MERGE:
            groups[-1].append(f)
        else:
            groups.append([f])
    return [(float(g[0]["t"]), "dead" if any(x["state"] == "dead" for x in g) else "down") for g in groups]


def _vote(frames: list[dict], key: str, allowed: tuple) -> str:
    """多张截图里 AI 读到的文字投票，只认表里的值（避免模型自己编出来的地图名）。
    长的先匹配：“撤离失败”不会被当成“失败”。"""
    ordered = sorted(allowed, key=len, reverse=True)
    votes: Counter = Counter()
    for f in frames:
        text = str(f.get(key) or "")
        hit = next((value for value in ordered if value in text), "")
        if hit:
            votes[hit] += 1
    return votes.most_common(1)[0][0] if votes else ""
