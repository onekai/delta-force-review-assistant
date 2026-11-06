"""社区改枪码：从 B 站公开视频的简介里抓取，按热度、发布时间和失效反馈打分，再按你的复盘数据排序。

- 只读公开接口（视频信息、相关推荐、热门评论），每次请求间隔 0.6 秒，一次抓取约 40 次请求。
- 遇到 B 站的验证码或风控就停下，不尝试绕过。
- 只收录完整格式的改枪码“枪名-烽火地带/全面战场-编码”，这是游戏里“复制改枪码”生成的格式，可以直接导入。
- 本软件不生成、不修改改枪码，也不会往游戏里粘贴。
"""

from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

SEED_BVIDS = ["BV1zCD4BvEML", "BV13rSdBiEvS", "BV1K9EW6FEcS", "BV1FiLC6aEuh", "BV1XVXzBhExJ", "BV1uPQeBgETo",
              "BV1cwoVBUEUH", "BV1E39KBmEKb", "BV1YEdDBBEvD", "BV1jrX5BKE3Z", "BV1NfLt6hEuC"]
API = "https://api.bilibili.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/126.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
}
REQUEST_GAP = 0.6
MAX_REQUESTS = 45
MAX_AGE_DAYS = 180
COMMENT_VIDEOS = 15

_KINDS = "紧凑突击步枪|突击步枪|战斗步枪|冲锋枪|轻机枪|通用机枪|机枪|射手步枪|狙击步枪|霰弹枪|手枪|卡宾枪|步枪"
_SEP = r"\s:：，,。|｜【】\[\]()（）、》>"
# 枪名的几种写法：字母数字开头（KC17突击步枪、SR-3M紧凑突击步枪、AS Val突击步枪）；已知的中文枪名（汤姆逊冲锋枪）；
# 行首或标点后的三个汉字加枪种；两个汉字加枪种（腾龙突击步枪）。这样“改装方案KC17突击步枪-…”不会把“改装方案”算进枪名。
CODE_RE = re.compile(
    r"(?P<weapon>[A-Za-z0-9][A-Za-z0-9\-. ]{0,14}?[\u4e00-\u9fa5]{0,6}"
    r"|(?:汤姆逊|腾龙|勇士|野牛|蝎式)(?:" + _KINDS + r")"
    r"|(?<![^" + _SEP + r"])[\u4e00-\u9fa5]{3}(?:" + _KINDS + r")"
    r"|[\u4e00-\u9fa5]{2}(?:" + _KINDS + r"))"
    r"-(?P<mode>烽火地带|全面战场)-(?P<code>[0-9A-Z]{18,32})(?![0-9A-Za-z])")
_LIST_NO = re.compile(r"^\d{1,2}[.、)）]\s*")  # 简介里的“1.”“2、”这类序号
# 价格：“24万”“18W”“28.5w”，或者“价格：285000”这种直接写哈夫币的；“10万播放”“1万粉丝”这类不算
BUDGET_RE = re.compile(r"(\d{1,3}(?:\.\d{1,2})?)\s*(?:万|[wW](?![a-zA-Z]))(?!\s*(?:播放|粉|赞|点赞|观看|收藏|次|人|浏览))")
PLAIN_PRICE_RE = re.compile(r"(?:价格|造价|成本|花费|预算|售价|总价|仅需|只要|哈夫币)\s*[:：约]?\s*(\d{5,7})(?!\d)")
PRICE_MIN, PRICE_MAX = 0.5, 300.0  # 单位：万哈夫币
INVALID_RE = re.compile(r"无效|失效|用不了|不能用|导入失败|导不进|过期了|不行了|码没了|已经没了")
GAME_HINT = re.compile(r"三角洲|烽火地带|全面战场|改枪|改装")

TAG_WORDS = {
    "压枪": ["压枪", "稳", "好压", "低后坐", "后坐力", "后座", "无后坐", "零后坐", "控枪", "不跳"],
    "高机动": ["机动", "开镜快", "瞄准速度", "轻量", "移速", "灵活"],
    "近战": ["近战", "腰射", "跑打", "贴脸", "冲锋", "近距离", "室内"],
    "远距离": ["远距离", "中远", "射程", "精确", "狙", "射手", "长距离"],
    "消音": ["消音", "静音", "无声"],
    "大容量": ["扩容", "大弹匣", "弹鼓", "大容量"],
    "性价比": ["性价比", "平民", "低价", "便宜", "穷", "白嫖", "低成本"],
    "满改": ["满改", "毕业", "顶配", "满配", "天花板"],
}
USE_RULES = [("近距离", {"近战"}), ("中距离", {"压枪"}), ("远距离", {"远距离"})]

ISSUE_TAGS = {"recoil": {"压枪"}, "slow": {"高机动", "近战"}, "crosshair": {"压枪"}, "outnumbered": {"大容量"},
              "exposed": {"高机动"}}
ISSUE_REASON = {"recoil": "补齐压枪短板", "slow": "补齐开镜慢的短板", "crosshair": "更稳，方便对准",
                "outnumbered": "大容量，一打多更从容", "exposed": "机动高，方便晃身"}
DISTANCE_USE = {"close": "近距离", "mid": "中距离", "long": "远距离"}
# 价格区间（单位万，含下限不含上限）。界面按这个顺序分组，价格没写的单独一组
BUDGETS = {"低于15万": (0, 15), "15万~30万": (15, 30), "30万~60万": (30, 60), "60万以上": (60, 10_000)}
UNKNOWN_BUCKET = "价格未标注"


def bucket_of(budget_wan) -> str:
    if budget_wan is None:
        return UNKNOWN_BUCKET
    b = float(budget_wan)
    return next((name for name, (lo, hi) in BUDGETS.items() if lo <= b < hi), UNKNOWN_BUCKET)


def price_label(budget_wan) -> str:
    if budget_wan is None:
        return ""
    b = float(budget_wan)
    return f"约 {b:g} 万" if b < 100 else f"约 {b:.0f} 万"


def find_price(text: str) -> float | None:
    """一行文字里的价格（万）。先把改枪码本身去掉，避免码里的“12W”被当成价格。"""
    t = CODE_RE.sub(" ", text or "")
    for m in BUDGET_RE.finditer(t):
        v = float(m.group(1))
        if PRICE_MIN <= v <= PRICE_MAX:
            return v
    m = PLAIN_PRICE_RE.search(t)
    if m:
        v = int(m.group(1)) / 10000
        if PRICE_MIN <= v <= PRICE_MAX:
            return round(v, 1)
    return None


class CrawlBlocked(Exception):
    pass


@dataclass
class Video:
    bvid: str
    aid: int
    title: str
    desc: str
    author: str
    pic: str
    pubdate: float
    views: int
    favorites: int
    codes: list = field(default_factory=list)
    invalid: int = 0


def _line_prices(lines: list[str]) -> dict[int, float]:
    """每一个改枪码所在行对应的价格：同一行 > 它“自己那一块”的相邻行 > 标题。
    简介常见两种写法：价格写在码的上一行（标题行），或者写在码的下一行；看第一个码之前、最后一个码之后哪边有价格来判断。"""
    code_idx = [i for i, line in enumerate(lines) if CODE_RE.search(line)]
    if not code_idx:
        return {}
    first, last = code_idx[0], code_idx[-1]
    before_first = any(find_price(lines[j]) for j in range(max(0, first - 2), first))
    after_last = any(find_price(lines[j]) for j in range(last + 1, min(len(lines), last + 3)))
    below = after_last and not before_first
    out: dict[int, float] = {}
    for k, i in enumerate(code_idx):
        price = find_price(lines[i])
        if price is None and below:
            nxt = code_idx[k + 1] if k + 1 < len(code_idx) else len(lines)
            price = next((p for j in range(i + 1, min(nxt, i + 3)) if (p := find_price(lines[j])) is not None), None)
        elif price is None:
            prv = code_idx[k - 1] if k > 0 else -1
            price = next((p for j in range(i - 1, max(prv, i - 3), -1) if (p := find_price(lines[j])) is not None), None)
        if price is not None:
            out[i] = price
    return out


def parse_codes(text: str, title: str = "") -> list[dict]:
    """从一段简介里找出完整格式的改枪码，并读出价格（同一行或相邻行，没有就看标题）和用途关键词。"""
    out, seen = [], set()
    lines = (text or "").splitlines() or [""]
    prices = _line_prices(lines)
    title_prices = [float(m.group(1)) for m in BUDGET_RE.finditer(title or "")
                    if PRICE_MIN <= float(m.group(1)) <= PRICE_MAX]
    title_price = title_prices[0] if len(set(title_prices)) == 1 else None  # 标题里只有一个价格才用
    for i, line in enumerate(lines):
        for m in CODE_RE.finditer(line):
            weapon = _LIST_NO.sub("", m.group("weapon")).strip()
            if len(weapon) < 2:
                continue
            full = f"{weapon}-{m.group('mode')}-{m.group('code')}"
            if full in seen:
                continue
            seen.add(full)
            context = line + " " + title
            budget = prices.get(i, title_price)
            tags = [tag for tag, words in TAG_WORDS.items() if any(w in context for w in words)]
            uses = [use for use, need in USE_RULES if need & set(tags)]
            out.append({"code": full, "weapon": weapon, "mode": m.group("mode"), "budget_wan": budget,
                        "tags": tags, "uses": uses})
    return out


_BVID = re.compile(r"^BV[0-9A-Za-z]{10}$")


def _video_from(item: dict) -> Video | None:
    if not item or not _BVID.match(str(item.get("bvid") or "")):
        return None
    stat = item.get("stat") or {}
    v = Video(bvid=item["bvid"], aid=int(item.get("aid") or 0), title=str(item.get("title") or ""),
              desc=str(item.get("desc") or ""), author=str((item.get("owner") or {}).get("name") or ""),
              pic=str(item.get("pic") or ""), pubdate=float(item.get("pubdate") or 0),
              views=int(stat.get("view") or 0), favorites=int(stat.get("favorite") or 0))
    if GAME_HINT.search(v.title + v.desc) or CODE_RE.search(v.desc):
        v.codes = parse_codes(v.desc, v.title)
    return v


class Crawler:
    def __init__(self, client: httpx.Client | None = None, gap: float = REQUEST_GAP, cancelled=None):
        self.client = client or httpx.Client(headers=HEADERS, timeout=15, follow_redirects=True)
        self.gap = gap
        self.requests = 0
        self.cancelled = cancelled or (lambda: False)
        self.comments_blocked = False
        self._last = 0.0

    def get(self, path: str, params: dict, critical: bool = True) -> dict | list | None:
        """critical=False 的请求（评论）被风控时只跳过，不中断整次抓取。"""
        if self.requests >= MAX_REQUESTS or self.cancelled():
            return None
        wait = self.gap - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        self.requests += 1
        try:
            resp = self.client.get(API + path, params=params)
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.info("B站请求失败 %s：%s", path, type(exc).__name__)
            return None
        code = body.get("code")
        if code in (-352, -412) or "v_voucher" in str(body.get("data"))[:500]:
            if not critical:
                self.comments_blocked = True
                return None
            raise CrawlBlocked("B 站暂时限制了访问（需要验证），已停止抓取，稍后再试")
        if code != 0:
            return None
        return body.get("data")

    def video(self, bvid: str) -> Video | None:
        data = self.get("/x/web-interface/view", {"bvid": bvid})
        return _video_from(data) if isinstance(data, dict) else None

    def related(self, bvid: str) -> list[Video]:
        data = self.get("/x/web-interface/archive/related", {"bvid": bvid})
        return [v for v in (_video_from(x) for x in (data or [])) if v] if isinstance(data, list) else []

    def invalid_mentions(self, aid: int) -> int:
        """热门评论里说“失效、用不了”的条数。"""
        if self.comments_blocked:
            return 0
        data = self.get("/x/v2/reply", {"type": 1, "oid": aid, "sort": 1, "ps": 20, "pn": 1}, critical=False)
        replies = (data.get("replies") or []) if isinstance(data, dict) else []
        return sum(1 for r in replies if INVALID_RE.search(((r.get("content") or {}).get("message") or "")))


def crawl(seeds: list[str], crawler: Crawler | None = None, progress=None, now: float | None = None) -> tuple[dict[str, Video], str]:
    """返回 ({bvid: 视频}, 提示信息)。被风控时返回已经抓到的部分。"""
    now = now or time.time()
    crawler = crawler or Crawler()
    videos: dict[str, Video] = {}
    note = ""
    queue = list(dict.fromkeys(seeds))
    expanded = 0

    def add(v: Video | None) -> None:
        if v and (v.bvid not in videos or len(v.codes) > len(videos[v.bvid].codes)):
            videos[v.bvid] = v

    try:
        for i, bvid in enumerate(queue):
            if crawler.requests >= MAX_REQUESTS - COMMENT_VIDEOS or crawler.cancelled():
                break
            if i < len(seeds):
                add(crawler.video(bvid))
            rel = crawler.related(bvid)
            for v in rel:
                add(v)
            expanded += 1
            # 第二层：较新的、带改枪码的相关视频也展开一次
            fresh = [v for v in rel if v.codes and now - v.pubdate < 60 * 86400 and v.bvid not in queue]
            fresh.sort(key=lambda v: v.views, reverse=True)
            queue += [v.bvid for v in fresh[:2]]
            if progress:
                progress(min(0.7, 0.7 * (i + 1) / max(1, len(seeds) + 6)))
        with_codes = sorted((v for v in videos.values() if v.codes), key=lambda v: v.views, reverse=True)
        for k, v in enumerate(with_codes[:COMMENT_VIDEOS]):
            if not v.aid or crawler.comments_blocked:
                continue
            v.invalid = crawler.invalid_mentions(v.aid)
            if progress:
                progress(0.7 + 0.3 * (k + 1) / COMMENT_VIDEOS)
    except CrawlBlocked as exc:
        note = str(exc)
    return videos, note


def store(db, videos: dict[str, Video], now: float | None = None) -> tuple[int, int]:
    """写入改枪码缓存。返回 (改枪码数, 来源视频数)。"""
    now = now or time.time()
    best: dict[str, tuple[Video, dict]] = {}
    sources: dict[str, set] = {}
    for v in videos.values():
        for c in v.codes:
            sources.setdefault(c["code"], set()).add(v.bvid)
            if c["code"] not in best or v.views > best[c["code"]][0].views:
                best[c["code"]] = (v, c)
    for code, (v, c) in best.items():
        old = db.one("SELECT first_seen FROM codes WHERE code=?", (code,))
        db.execute(
            "INSERT INTO codes(code,weapon,mode,title,author,bvid,url,cover,pubdate,views,favorites,invalid,sources,"
            "first_seen,last_seen,uses,budget_wan) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET weapon=excluded.weapon, mode=excluded.mode, title=excluded.title, "
            "author=excluded.author, bvid=excluded.bvid, url=excluded.url, cover=excluded.cover, pubdate=excluded.pubdate, "
            "views=excluded.views, favorites=excluded.favorites, invalid=excluded.invalid, sources=excluded.sources, "
            "last_seen=excluded.last_seen, uses=excluded.uses, budget_wan=excluded.budget_wan",
            (code, c["weapon"], c["mode"], v.title, v.author, v.bvid, f"https://www.bilibili.com/video/{v.bvid}",
             _cover(v.pic), v.pubdate, v.views, v.favorites, v.invalid, len(sources[code]),
             old["first_seen"] if old else now, now,
             "|".join(c["uses"]) + "#" + "|".join(c["tags"]), c["budget_wan"]))
    return len(best), sum(1 for v in videos.values() if v.codes)


def _cover(pic: str) -> str:
    if not pic:
        return ""
    pic = pic.replace("http://", "https://")
    return pic + "@480w_270h_1c.jpg" if "hdslb.com" in pic and "@" not in pic else pic


def _wan(n: int) -> str:
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    if n >= 1000:
        return f"{n / 1000:.1f}千"
    return str(n)


def _age(pubdate: float, now: float) -> str:
    days = int((now - pubdate) // 86400) if pubdate else -1
    if days < 0:
        return "—"
    if days == 0:
        return "今天"
    return f"{days} 天前" if days < 60 else f"{days // 30} 个月前"


def base_score(row: dict, fb_ok: int, fb_fail: int, now: float) -> float:
    days = max(0.0, (now - float(row.get("pubdate") or now)) / 86400)
    heat = math.log10((row.get("views") or 0) + 1) * 10 + math.log10((row.get("favorites") or 0) + 1) * 6
    score = heat * (0.35 + 0.65 * math.exp(-days / 45))
    score += -8 * int(row.get("invalid") or 0) + 6 * fb_ok - 10 * fb_fail + 5 * max(0, int(row.get("sources") or 1) - 1)
    return max(0.0, min(100.0, score))


def ranked(db, issue: str = "", distance: str = "", budget: str = "", weapon: str = "", mode: str = "", use: str = "",
           now: float | None = None) -> list[dict]:
    """按综合分排序的改枪码（已按筛选条件过滤）。issue/distance 来自你的复盘数据。"""
    now = now or time.time()
    fb = {r["code"]: (r["ok"] or 0, r["fail"] or 0) for r in db.query(
        "SELECT code, SUM(ok) AS ok, SUM(1-ok) AS fail FROM code_feedback GROUP BY code")}
    adopted = {r["code"] for r in db.query("SELECT code FROM adoptions")}
    lo, hi = BUDGETS.get(budget, (None, None))
    want_tags = ISSUE_TAGS.get(issue, set())
    want_use = DISTANCE_USE.get(distance, "")
    out = []
    for row in db.query("SELECT * FROM codes"):
        if row.get("pubdate") and now - float(row["pubdate"]) > MAX_AGE_DAYS * 86400:
            continue
        uses_s, _, tags_s = (row.get("uses") or "").partition("#")
        uses = [u for u in uses_s.split("|") if u]
        tags = [t for t in tags_s.split("|") if t]
        if weapon and row["weapon"] != weapon or mode and row["mode"] != mode or use and use not in uses:
            continue
        b = row.get("budget_wan")
        if lo is not None and b is not None and not (lo <= float(b) < hi):
            continue
        ok, fail = fb.get(row["code"], (0, 0))
        score = base_score(row, ok, fail, now)
        reasons = []
        if want_tags & set(tags):
            score += 12
            reasons.append(ISSUE_REASON.get(issue, "对症"))
        if want_use and want_use in uses:
            score += 8
            reasons.append(f"适合{want_use}交战")
        if fail >= 2 and fail > ok:
            score -= 30
        out.append({
            "code": row["code"], "weapon": row["weapon"], "mode": row["mode"], "uses": uses, "tags": tags,
            "score": round(max(0.0, min(100.0, score))), "match_reason": "、".join(reasons) or "社区热度",
            "age_label": _age(float(row.get("pubdate") or 0), now),
            "popularity": f"{_wan(int(row.get('views') or 0))} / {_wan(int(row.get('favorites') or 0))}",
            "invalid": int(row.get("invalid") or 0), "author": row.get("author") or "", "url": row.get("url") or "",
            "source_title": row.get("title") or "", "cover": row.get("cover") or "", "fb_ok": ok, "fb_fail": fail,
            "adopted": row["code"] in adopted, "budget_wan": b, "matched": bool(want_tags & set(tags)),
            "bucket": bucket_of(b), "price_label": price_label(b),
        })
    out.sort(key=lambda c: (-c["score"], c["code"]))
    return out
