"""AI 看图：判断每隔几秒一张截图是什么画面（大厅 / 对局 / 被击倒 / 阵亡 / 结算……），据此按局切分、找出阵亡，
再对每次阵亡给出死因判断。看图模型可以选 DeepSeek / 通义千问 / 豆包 / 智谱（见 aiclient.py）。

- 边录边分析时只用最低优先级处理录完的分段，不弹窗、不叠加画面。
- 发给 AI 的只有缩小后的截图拼图，不发视频、不发账号信息。
- 每次调用前检查当天的用量上限，所有调用记入本地用量表。
- AI 的结论都标成“待确认”，你确认后才计入统计和建议。
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from .aiclient import AIClient, AIError, ChatResult
from .media import Timeline
from .stats import CAUSES

log = logging.getLogger(__name__)

SCAN_EVERY = 3.0
SCAN_COLS = SCAN_ROWS = 3
GROUP_SEC = 20.0
DEDUP_SEC = 15.0
EST_SCAN_TOKENS = 1400
EST_DIAG_TOKENS = 2200


class Cancelled(Exception):
    """游戏启动了，或用户取消了。"""


class BudgetExceeded(AIError):
    pass


STATES = ("lobby", "loading", "match", "map", "down", "dead", "result", "other")
STATE_CODES = {"lobby": "L", "loading": "O", "match": "M", "map": "P", "down": "D", "dead": "X", "result": "R", "other": "?"}
STATE_PROMPT = (
    "这是 PC 游戏《三角洲行动》的录像截图，按时间顺序排成 {cols}×{rows} 宫格，从左到右、从上到下编号 1～{n}，"
    "相邻两格大约间隔 {every:.0f} 秒（左上角的黑块是遮掉的账号水印，不用管）。\n"
    "请判断每一格是下面哪种画面，只用这些代号：\n"
    "lobby：大厅和各种菜单（顶部有“开始游戏、仓库、特战干员、交易行”之类的菜单，或组队、商店、改枪界面）\n"
    "loading：匹配中、加载画面、黑屏、过场动画、选出生点或部署界面\n"
    "match：正在对局，录像主人在行动或战斗（有准星、弹药数、小地图、血量等战斗界面）\n"
    "map：对局中打开了大地图、背包、搜刮物资或交易界面\n"
    "down：录像主人自己被击倒，倒在地上等待救援\n"
    "dead：录像主人自己阵亡（出现击杀者信息、“阵亡/被击败”等文字、观战队友、画面变灰）\n"
    "result：一局结束的结算界面（撤离成功、撤离失败、胜利、失败、带出价值、战绩统计）\n"
    "other：看不出来\n"
    "注意：录像主人击倒或击败别人的提示不算 down/dead。\n"
    "如果画面里能看清地图名（如零号大坝、长弓溪谷、航天基地、巴克什、潮汐监狱）、模式（烽火地带、全面战场）"
    "或结算结果（撤离成功、撤离失败、胜利、失败），也写出来，看不清就留空。\n"
    "只输出 JSON，不要解释：{{\"states\": [第 1 格到第 {n} 格的代号], \"map\": \"\", \"mode\": \"\", \"result\": \"\"}}"
)

REFINE_PROMPT = (
    "这是《三角洲行动》录像截图，按时间顺序排成 3×3 宫格，从左到右、从上到下编号 1～9，相邻两格间隔 1 秒。\n"
    "哪一格最先出现录像主人自己被击倒或阵亡的画面（屏幕变灰变暗、阵亡文字、击杀者信息卡、观战或部署界面）？\n"
    "只输出 JSON：{\"first\": 格子编号}，如果都不是就输出 {\"first\": 0}。"
)

DIAG_OFFSETS = [-6.0, -3.0, -1.5, -1.0, -0.5, -0.2]
DIAGNOSE_PROMPT = (
    "第一张图是 PC 游戏《三角洲行动》里录像主人阵亡前的 6 个时刻，排成 3×2 宫格，从左到右、从上到下依次是阵亡前"
    "约 6、3、1.5、1、0.5、0.2 秒。第二张图是阵亡后约 1.5 秒的画面，可能带有击杀者信息。\n"
    "请判断这次阵亡最主要的原因，只能从下面选一个代号：\n"
    "unseen 没先看到人：被打之前画面里没有出现敌人，或敌人在离准星很远的地方先开火\n"
    "crosshair 预瞄位置偏了：看到了敌人，但准星离敌人很远，需要大幅度甩枪\n"
    "slow 开火太晚：敌人出现时准星离得不远，但开镜或开火明显比对方慢\n"
    "recoil 压枪散了：持续开火时准星明显上飘或乱晃，子弹打不中\n"
    "exposed 身位暴露：站在空旷处、整个身体露在掩体外，或反复从同一个位置探头\n"
    "outnumbered 被多打少：同时有两个以上敌人在打录像主人\n"
    "gear 装备劣势：明显打中了对方却打不死（对方护甲更好或子弹等级低）\n"
    "other 其他：手雷、载具、坠落，或画面看不出原因\n"
    "然后只输出下面格式的 JSON，不要解释。看不清的字段填空字符串，数字看不清填 null，不要编造：\n"
    "{\"cause\": \"代号\", \"headline\": \"一句话结论，15 字以内\", \"process\": \"交火过程，50 字以内\", "
    "\"process_tag\": \"失误 或 空字符串\", \"gear\": \"伤害与装备，40 字以内\", \"gear_tag\": \"劣势 或 空字符串\", "
    "\"awareness\": \"听声与视野，40 字以内\", \"awareness_tag\": \"失误 或 空字符串\", "
    "\"killer_weapon\": \"击杀者武器名\", \"distance_m\": 交战距离米数, "
    "\"advice\": \"下一次怎么做，40 字以内\", \"confidence\": 0到1之间的数字}"
)


def parse_json(text: str) -> dict:
    """模型有时会在 JSON 外面包代码块或多说几句，这里只取第一个 {...}。"""
    if not text:
        return {}
    text = re.sub(r"```(?:json)?", "", text)
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start:i + 1])
                        return value if isinstance(value, dict) else {}
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return {}


def _int_list(value, n: int) -> list[int]:
    out = []
    for v in value if isinstance(value, list) else []:
        try:
            k = int(v)
        except (TypeError, ValueError):
            continue
        if 1 <= k <= n and k not in out:
            out.append(k)
    return out


def group_times(times: list[float], gap: float = GROUP_SEC) -> list[float]:
    """阵亡画面会持续好几秒，相邻的命中合并成一次，取最早的时间。"""
    groups: list[float] = []
    last = None
    for t in sorted(times):
        if last is None or t - last > gap:
            groups.append(t)
        last = t
    return groups


class AISession:
    """一次分析任务用的 AI 连接：带用量上限检查和重试。"""

    def __init__(self, api_key: str, base_url: str, model: str, db, daily_limit: int, cancelled=None,
                 transport=None, provider: str = "deepseek"):
        self.client = AIClient(api_key, base_url, provider=provider, timeout=90, transport=transport)
        self.model = model
        self.db = db
        self.daily_limit = daily_limit
        self.cancelled = cancelled or (lambda: False)

    def close(self) -> None:
        self.client.close()

    def check(self, estimate: int) -> None:
        if self.cancelled():
            raise Cancelled()
        if self.daily_limit and self.db.usage_today() + estimate > self.daily_limit:
            raise BudgetExceeded("今天的 AI 用量已到上限（可以在设置里调高），明天再继续")

    def ask(self, purpose: str, prompt: str, images: list[bytes], estimate: int, max_tokens: int = 400) -> ChatResult:
        content = [{"type": "text", "text": prompt}] + [self.client.image_part(img) for img in images]
        for attempt in range(3):
            self.check(estimate)
            try:
                result = self.client.chat(self.model, [{"role": "user", "content": content}],
                                          max_tokens=max_tokens, temperature=0.1)
            except AIError as exc:
                if exc.status in (429, 500, 503) and attempt < 2:
                    time.sleep(3 * (attempt + 1))
                    continue
                raise
            self.db.add_usage(result.model, purpose, result.prompt_tokens, result.completion_tokens)
            return result
        raise AIError("AI 服务一直繁忙，请稍后再试")


def classify_sheet(ai: AISession, sheet: dict, cols: int = SCAN_COLS, rows: int = SCAN_ROWS) -> dict:
    """一张拼图 → 每格的画面状态，以及读到的地图 / 模式 / 结算结果。"""
    n = len(sheet["times"])
    prompt = STATE_PROMPT.format(cols=cols, rows=rows, n=n, every=SCAN_EVERY)
    result = ai.ask("states", prompt, [Path(sheet["path"]).read_bytes()], EST_SCAN_TOKENS, max_tokens=400)
    data = parse_json(result.text)
    raw = data.get("states") if isinstance(data.get("states"), list) else []
    states = [str(x).strip().lower() if str(x).strip().lower() in STATES else "other" for x in raw[:n]]
    states += ["other"] * (n - len(states))
    log.info("AI 画面状态 %s：%s %s", Path(sheet["path"]).name, "".join(STATE_CODES.get(x, "?") for x in states),
             {k: data.get(k) for k in ("map", "mode", "result") if data.get(k)})
    return {"states": states, "map": str(data.get("map") or "")[:20], "mode": str(data.get("mode") or "")[:10],
            "result": str(data.get("result") or "")[:10]}


def refine(ai: AISession, timeline: Timeline, t: float) -> float:
    """粗筛的精度是 3 秒，这里在前后各几秒按 1 秒间隔再看一次，找到阵亡画面最早出现的那一秒。"""
    times = [max(0.0, t - 6 + i) for i in range(9)]
    img = timeline.grid(times, cols=3, tile_width=480)
    if not img:
        return t
    try:
        data = parse_json(ai.ask("refine", REFINE_PROMPT, [img], EST_SCAN_TOKENS, max_tokens=120).text)
    except AIError as exc:
        if isinstance(exc, BudgetExceeded):
            raise
        log.info("精确定位失败，使用粗筛时间：%s", exc)
        return t
    try:
        k = int(data.get("first") or 0)
    except (TypeError, ValueError):
        k = 0
    return times[k - 1] if 1 <= k <= 9 else t


def clean_diagnosis(data: dict, model: str) -> dict:
    cause = str(data.get("cause") or "").strip().lower()
    if cause not in CAUSES:
        cause = "other"

    def s(key: str, limit: int = 80) -> str:
        value = data.get(key)
        return str(value).strip()[:limit] if value not in (None, "null") else ""

    try:
        distance = float(data.get("distance_m"))
        distance = distance if 0 < distance < 1000 else None
    except (TypeError, ValueError):
        distance = None
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence"))))
    except (TypeError, ValueError):
        confidence = None
    return {"cause": cause, "headline": s("headline", 30), "process": s("process"), "process_tag": s("process_tag", 6),
            "gear": s("gear"), "gear_tag": s("gear_tag", 6), "awareness": s("awareness"),
            "awareness_tag": s("awareness_tag", 6), "killer_weapon": s("killer_weapon", 24), "distance_m": distance,
            "advice": s("advice"), "confidence": confidence, "model": model}


def diagnose(ai: AISession, timeline: Timeline, t: float, offset: float = 0.0) -> dict:
    """t 是阵亡时间（录像时间）；offset 用于只剩片段时把录像时间换成片段内时间。"""
    local = t - offset
    grid = timeline.grid([max(0.0, local + d) for d in DIAG_OFFSETS], cols=3, tile_width=480)
    after = timeline.frame(local + 1.5, width=1600, mask=True)
    if not grid:
        raise AIError("这个时间点截不出画面（原片可能已清理）")
    images = [grid] + ([after] if after else [])
    try:
        result = ai.ask("diagnose", DIAGNOSE_PROMPT, images, EST_DIAG_TOKENS, max_tokens=900)
    except AIError as exc:
        if exc.status == 400 and len(images) > 1:  # 有的模型一次只收一张图
            result = ai.ask("diagnose", DIAGNOSE_PROMPT.replace("第二张图", "（没有第二张图）"), [grid],
                            EST_DIAG_TOKENS, max_tokens=900)
        else:
            raise
    data = parse_json(result.text)
    if not data:
        raise AIError("AI 返回的内容看不懂，请稍后重试")
    return clean_diagnosis(data, result.model)
