"""本地 SQLite 数据库：标注、AI 判断、片段、改枪码缓存、帧率测量、优化备份、AI 用量。

单个连接 + 一把锁，后台线程和界面线程都可以安全调用。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS session_meta(
  name TEXT PRIMARY KEY, mode TEXT DEFAULT '', map TEXT DEFAULT '', result TEXT DEFAULT '',
  source_path TEXT DEFAULT '', imported INTEGER DEFAULT 0, started_at REAL, duration REAL,
  ai_scanned_at REAL, updated REAL);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL, t REAL NOT NULL, kind TEXT NOT NULL,
  cause TEXT DEFAULT '', source TEXT DEFAULT 'manual', confirmed INTEGER DEFAULT 0,
  killer_weapon TEXT DEFAULT '', distance_m REAL, ai_json TEXT, clip_path TEXT, clip_start REAL, clip_end REAL,
  created REAL);
CREATE INDEX IF NOT EXISTS ix_events_session ON events(session);
CREATE TABLE IF NOT EXISTS clips(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT, path TEXT, t_start REAL, t_end REAL, created REAL);
CREATE TABLE IF NOT EXISTS recommendations(
  id INTEGER PRIMARY KEY AUTOINCREMENT, rule TEXT, title TEXT, evidence TEXT, how TEXT, change_text TEXT,
  verify TEXT, status TEXT, created REAL, adopted_at REAL, baseline TEXT, result TEXT, closed_at REAL);
CREATE TABLE IF NOT EXISTS codes(
  code TEXT PRIMARY KEY, weapon TEXT, mode TEXT, title TEXT, author TEXT, bvid TEXT, url TEXT, cover TEXT,
  pubdate REAL, views INTEGER, favorites INTEGER, invalid INTEGER DEFAULT 0, sources INTEGER DEFAULT 1,
  first_seen REAL, last_seen REAL, uses TEXT, budget_wan REAL);
CREATE TABLE IF NOT EXISTS code_feedback(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, ok INTEGER, ts REAL);
CREATE TABLE IF NOT EXISTS adoptions(code TEXT PRIMARY KEY, weapon TEXT, adopted_at REAL);
CREATE TABLE IF NOT EXISTS measurements(
  id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL, label TEXT, process TEXT, seconds REAL, frames INTEGER,
  avg REAL, low1 REAL, p99 REAL, series TEXT);
CREATE TABLE IF NOT EXISTS optimizer_backup(key TEXT PRIMARY KEY, original TEXT, applied TEXT, applied_at REAL);
CREATE TABLE IF NOT EXISTS ai_usage(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, model TEXT, purpose TEXT, prompt_tokens INTEGER, completion_tokens INTEGER);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS frame_states(
  session TEXT NOT NULL, t REAL NOT NULL, state TEXT, info TEXT, PRIMARY KEY(session, t));
CREATE TABLE IF NOT EXISTS seg_done(
  session TEXT NOT NULL, seg TEXT NOT NULL, ai INTEGER DEFAULT 0, done_at REAL, PRIMARY KEY(session, seg));
CREATE TABLE IF NOT EXISTS matches(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL, t_start REAL, t_end REAL, closed INTEGER DEFAULT 0,
  map TEXT DEFAULT '', mode TEXT DEFAULT '', result TEXT DEFAULT '', deaths_json TEXT, analyzed_at REAL, created REAL,
  coach TEXT);
CREATE INDEX IF NOT EXISTS ix_matches_session ON matches(session);
"""


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            for table, column, kind in (("matches", "coach", "TEXT"),):  # 旧版本数据库补新列
                cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if column not in cols:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
            self._conn.commit()

    # ---- 基础 ----
    def execute(self, sql: str, params=()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query(self, sql: str, params=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params=()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def backup_to(self, target: Path) -> None:
        """导出一份完整、一致的数据库副本。"""
        with self._lock, sqlite3.connect(str(target)) as dst:
            self._conn.backup(dst)

    # ---- kv ----
    def kv_get(self, key: str, default=None):
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except ValueError:
            return default

    def kv_set(self, key: str, value) -> None:
        self.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, json.dumps(value, ensure_ascii=False)))

    # ---- 场次信息 ----
    def meta(self, name: str) -> dict:
        return self.one("SELECT * FROM session_meta WHERE name=?", (name,)) or {
            "name": name, "mode": "", "map": "", "result": "", "source_path": "", "imported": 0}

    def set_meta(self, name: str, **fields) -> None:
        allowed = {"mode", "map", "result", "source_path", "imported", "started_at", "duration", "ai_scanned_at"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        self.execute("INSERT OR IGNORE INTO session_meta(name, updated) VALUES(?,?)", (name, time.time()))
        for key, value in fields.items():
            self.execute(f"UPDATE session_meta SET {key}=?, updated=? WHERE name=?", (value, time.time(), name))

    # ---- 事件（阵亡 / 对枪） ----
    def add_event(self, session: str, t: float, kind: str, source: str = "manual", cause: str = "",
                  confirmed: bool = False, **extra) -> int:
        cur = self.execute(
            "INSERT INTO events(session,t,kind,cause,source,confirmed,killer_weapon,distance_m,ai_json,created) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (session, float(t), kind, cause, source, int(confirmed), extra.get("killer_weapon", ""),
             extra.get("distance_m"), json.dumps(extra["ai"], ensure_ascii=False) if extra.get("ai") else None,
             time.time()))
        return int(cur.lastrowid)

    def update_event(self, event_id: int, **fields) -> None:
        allowed = {"cause", "confirmed", "killer_weapon", "distance_m", "ai_json", "clip_path", "clip_start", "clip_end",
                   "t", "source"}
        for key, value in fields.items():
            if key in allowed:
                self.execute(f"UPDATE events SET {key}=? WHERE id=?", (value, event_id))

    def event(self, event_id: int) -> dict | None:
        return self.one("SELECT * FROM events WHERE id=?", (event_id,))

    def events(self, session: str | None = None) -> list[dict]:
        if session is None:
            return self.query("SELECT * FROM events ORDER BY session, t")
        return self.query("SELECT * FROM events WHERE session=? ORDER BY t", (session,))

    def delete_event(self, event_id: int) -> None:
        self.execute("DELETE FROM events WHERE id=?", (event_id,))

    # ---- 画面状态与按局切分 ----
    def add_states(self, session: str, rows: list[tuple[float, str, dict]]) -> None:
        with self._lock:
            self._conn.executemany(
                "INSERT INTO frame_states(session,t,state,info) VALUES(?,?,?,?) "
                "ON CONFLICT(session,t) DO UPDATE SET state=excluded.state, info=excluded.info",
                [(session, float(t), state, json.dumps(info, ensure_ascii=False) if info else None) for t, state, info in rows])
            self._conn.commit()

    def states(self, session: str) -> list[dict]:
        out = []
        for r in self.query("SELECT t, state, info FROM frame_states WHERE session=? ORDER BY t", (session,)):
            info = json.loads(r["info"]) if r["info"] else {}
            out.append({"t": r["t"], "state": r["state"], **info})
        return out

    def seg_is_done(self, session: str, seg: str, need_ai: bool) -> bool:
        row = self.one("SELECT ai FROM seg_done WHERE session=? AND seg=?", (session, seg))
        return bool(row) and (bool(row["ai"]) or not need_ai)

    def mark_seg(self, session: str, seg: str, ai: bool) -> None:
        self.execute("INSERT INTO seg_done(session,seg,ai,done_at) VALUES(?,?,?,?) "
                     "ON CONFLICT(session,seg) DO UPDATE SET ai=MAX(ai, excluded.ai), done_at=excluded.done_at",
                     (session, seg, int(ai), time.time()))

    def matches(self, session: str | None = None) -> list[dict]:
        if session is None:
            return self.query("SELECT * FROM matches ORDER BY session, t_start")
        return self.query("SELECT * FROM matches WHERE session=? ORDER BY t_start", (session,))

    def match(self, match_id: int) -> dict | None:
        return self.one("SELECT * FROM matches WHERE id=?", (match_id,))

    # ---- AI 用量 ----
    def add_usage(self, model: str, purpose: str, prompt_tokens: int, completion_tokens: int) -> None:
        self.execute("INSERT INTO ai_usage(ts,model,purpose,prompt_tokens,completion_tokens) VALUES(?,?,?,?,?)",
                     (time.time(), model, purpose, prompt_tokens, completion_tokens))

    def usage_today(self, now: float | None = None) -> int:
        now = now or time.time()
        lt = time.localtime(now)
        start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        row = self.one("SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) AS n FROM ai_usage WHERE ts>=?", (start,))
        return int(row["n"]) if row else 0

    # ---- 统计 ----
    def stats(self) -> dict:
        count = lambda table: self.one(f"SELECT COUNT(*) AS n FROM {table}")["n"]  # noqa: E731
        return {"events": count("events"), "codes": count("codes"), "measurements": count("measurements"),
                "size": self.path.stat().st_size if self.path.exists() else 0}

    def reset(self) -> None:
        with self._lock:
            for table in ("events", "clips", "recommendations", "codes", "code_feedback", "adoptions",
                          "measurements", "ai_usage", "kv", "session_meta", "frame_states", "seg_done", "matches"):
                self._conn.execute(f"DELETE FROM {table}")
            self._conn.commit()
