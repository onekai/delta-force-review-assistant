"""后台：把录制、录像处理、AI、建议、改枪码和赛前优化接起来，供界面调用。

界面通过 call(方法名, 参数) 调用，返回 {"ok": True, "data": ...} 或 {"ok": False, "error": 中文提示}。
耗时的事放进后台任务（jobs.py），界面每秒读一次 state 看进度。
这个文件不依赖 Qt：界面外壳通过 host 提供“打开文件夹、打开网页、复制”这些能力，单元测试可以直接用。
"""

from __future__ import annotations

import csv
import hashlib
import inspect
import io
import json
import logging
import shutil
import sys
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse

from . import (BUILD, __version__, aiclient, analysis, config, diagnostics, encoders, gunsmith, hwinfo, media, ocr,
               optimizer, paths, perfmon, recommend, secrets_store, sessions, stats, winapi)
from . import matches as matchlib
from .db import DB
from .aiclient import HUD_TEST_PROMPT, PROVIDERS, AIClient, AIError, pick_default_models
from .game_watch import GameState, GameWatcher, describe_windows_for_picker
from .gpu_stats import NvidiaStats
from .jobs import JobCancelled, JobError, Jobs
from .recorder import RecordingController, free_gb

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
MODES = {"": "未标注", "extraction": "烽火地带", "warfare": "全面战场"}
RESULTS = {"": "未标注", "extracted": "撤离成功", "failed": "撤离失败", "win": "胜利", "loss": "失败"}
EVENT_KINDS = {"death", "duel_win", "duel_loss"}
PROFILE_LIMITS = {"dpi": (50, 64000), "mouse": (0.01, 100), "vertical": (0.01, 100), "horizontal": (0.01, 100),
                  "ads_boost": (0.01, 10), "ads_vertical": (0.01, 10), "ads_horizontal": (0.01, 10), "fov": (50, 150)}
# 关掉“边录边分析”时，游戏一启动就停下来的任务，关掉游戏后自动继续
PAUSE_IN_GAME = {"post", "segments", "match", "ai_scan", "ai_diagnose", "thumbs"}
SEG_CHECK_SEC = 5  # 每 5 秒看一次有没有录完的新分段
AI_PAUSE_SEC = 600  # AI 出错（余额不足、密钥失效等）后暂停 10 分钟再试，避免反复报错
VISION_RETRY_SEC = 3600
OPEN_URL_HOSTS = {"www.bilibili.com", "www.nvidia.cn", "www.amd.com", "www.intel.cn", "platform.deepseek.com",
                  "github.com", "bailian.console.aliyun.com", "console.volcengine.com", "open.bigmodel.cn"}
BUDGET_OPTIONS = {""} | set(gunsmith.BUDGETS)
# 这些方法比较慢，界面外壳会放到后台线程里执行，避免界面卡住
THREADED = {"dashboard", "matches", "session_detail", "gunsmith", "optimizer", "settings", "optimize_apply",
            "optimize_restore", "ai_test", "export_data", "export_diagnostics", "import_video", "reset_data"}
# 需要先弹出系统文件选择框的方法（由界面外壳负责弹框，再把选中的路径放进 path 参数）
DIALOGS = {"pick_folder": ("folder", "选择录像保存位置"), "import_video": ("video", "选择要导入的录像文件"),
           "ai_test:image-file": ("image", "选择一张游戏截图")}
DIRECTION = {"recoil": ("优先压枪稳定", "后坐力低、枪身稳的改法加分"), "slow": ("优先开镜快、机动高", "近距离先手更容易"),
             "crosshair": ("优先稳定好瞄", "准星更稳，定位误差更小"), "outnumbered": ("优先大容量", "一打多时不用频繁换弹"),
             "exposed": ("优先机动", "晃身探头更灵活")}


class UserError(Exception):
    """给用户看的错误，已经是中文。"""


class Host:
    """界面外壳提供的能力。默认实现只写日志，方便测试。"""

    def open_path(self, path: str) -> None:
        log.info("打开文件夹：%s", path)

    def open_url(self, url: str) -> None:
        log.info("打开网页：%s", url)

    def copy_text(self, text: str) -> None:
        log.info("复制：%s", text[:40])


def _name_time(name: str) -> float:
    """场次名里的时间（20261020_194210 或 import_20261020_194210）。"""
    stamp = name.split("_", 1)[1] if name.startswith("import_") else name
    try:
        return time.mktime(time.strptime(stamp[:15], "%Y%m%d_%H%M%S"))
    except ValueError:
        return 0.0


def _parse_clock(text: str) -> float:
    try:
        h, m, s = (text or "").split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except ValueError:
        return 0.0


def _fmt_clock(sec: float) -> str:
    sec = max(0.0, sec)
    return f"{int(sec // 3600):02d}:{int(sec % 3600 // 60):02d}:{sec % 60:04.1f}"


def _merge_state(o: tuple | None, a: tuple | None) -> tuple[str, dict]:
    """合并本机文字识别（o）和 AI（a）对同一帧的判断。大厅、结算这类有固定文字的，以文字识别为准；
    其余以 AI 为准；只有文字识别时，没认出特定文字就当作对局中。"""
    o_st, o_info = o if o else ("", {})
    a_st, a_info = a if a else ("", {})
    info = {**a_info, **o_info}
    info["src"] = "mix" if (o and a) else ("ai" if a else "ocr")
    if o_st in ("lobby", "result"):
        return o_st, info
    if a_st and a_st != "other":
        return a_st, info
    if o_st:
        return o_st, info
    if o is not None:
        return "match", info
    return a_st or "other", info


def _downsample(values: list, n: int) -> list:
    if not values or len(values) <= n:
        return list(values or [])
    step = len(values) / n
    return [max(values[int(i * step):int((i + 1) * step)] or [values[int(i * step)]]) for i in range(n)]


class Backend:
    def __init__(self, host: Host | None = None, cfg: config.Config | None = None):
        self.host = host or Host()
        self.cfg = cfg or config.load()
        self._cfg_lock = threading.RLock()
        self.db = DB(paths.data_dir() / "fupan.db")
        self.jobs = Jobs()
        self.watcher = GameWatcher(self.cfg)
        self.controller = RecordingController(self.cfg)
        self.opt = optimizer.Optimizer(self.db)
        self.gpu = NvidiaStats()
        self.hw: dict = {}
        self.encoder_report: list[dict] = []
        self.encoder_busy = False
        self.encoder_failed = ""  # 编码器自检失败的原因，界面上替换录制状态显示
        self.game = GameState(None, False)
        self.game_exe = self.db.kv_get("game_exe_name", "") or ""
        self._session_name: str | None = None
        self._storage = (0.0, {"used_bytes": 0, "free_bytes": 0, "path": str(self.cfg.recordings_path())})
        self._opt_cache: tuple[float, list] = (0.0, [])
        self._sessions_cache: tuple[float, list] = (0.0, [])
        self._sessions_lock = threading.Lock()
        self._key: str | None = None
        self._key_provider = ""
        self._fingerprint = ""
        self._exe_path: str | None = None
        self._started = False
        self._last_crawl_check = time.time()
        self._last_seg_check = 0.0
        self._ai_pause_until = 0.0
        self._ai_error = ""

    # =================================================================
    # 生命周期
    # =================================================================
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        running = self.watcher.poll().running
        if not running:  # 上次异常退出时，电源计划可能还停在“打游戏”状态
            restored = self.opt.restore_power(only_session=True)
            if restored:
                log.info("已把电源计划改回打游戏之前的设置")
        self.jobs.start("hw", "检测电脑配置", self._job_collect_hw, pool="sys", notify=False)
        self._cleanup_old()
        timer = threading.Timer(20, self._maybe_auto_crawl)
        timer.daemon = True
        timer.start()
        if not running:
            self._resume_pending()

    def shutdown(self) -> None:
        try:
            self.controller.shutdown()
        except Exception:
            log.exception("退出时停止录制出错")
        try:
            if self.cfg.restore_power_after_game:
                self.opt.restore_power(only_session=True)
        except Exception:
            log.exception("退出时还原电源计划出错")
        self.jobs.shutdown()

    def tick(self, now: float | None = None) -> None:
        """界面外壳每秒调用一次（在界面线程）。"""
        try:
            state = self.watcher.poll()
        except Exception:
            log.exception("读取游戏窗口出错")
            return
        was_running = self.game.running
        self.game = state
        if state.window and state.window.exe and state.window.exe != self.game_exe:
            self.game_exe = state.window.exe
            self.db.kv_set("game_exe_name", self.game_exe)
        if state.running and not was_running:
            self._on_game_start()
        try:
            self.controller.tick(state, now)
        except Exception:
            log.exception("录制状态更新出错")
        name = self.controller.session.name if self.controller.session else None
        if self._session_name and name != self._session_name:
            self._on_session_closed(self._session_name)
        self._session_name = name
        if was_running and not state.running:
            self._on_game_exit()
        if time.time() - self._last_crawl_check > 3600:  # 软件一直开着时，每小时看一次要不要更新改枪码
            self._last_crawl_check = time.time()
            threading.Thread(target=self._maybe_auto_crawl, daemon=True).start()
        if self.cfg.live_analyze and self.controller.session_dir is not None and \
                time.time() - self._last_seg_check >= SEG_CHECK_SEC:
            self._last_seg_check = time.time()
            self._check_live_segments()

    def _check_live_segments(self) -> None:
        """边录边分析：有录完、还没处理的分段，就交给后台（最低优先级）处理。"""
        session = self.controller.session.name if self.controller.session else ""
        if not session:
            return
        ai_on = self._ai_on()
        for f in self._completed_files(session):
            try:
                key = f"{f.name}|{f.stat().st_size}"
            except OSError:
                continue
            if not self.db.seg_is_done(session, key, need_ai=ai_on):
                self._start_segments(session, final=False)
                return

    def _on_game_start(self) -> None:
        log.info("检测到游戏启动")
        if not self.cfg.live_analyze:  # 没开“边录边分析”：对局中不做任何分析，关掉游戏后再继续
            for job in self.jobs.cancel(PAUSE_IN_GAME):
                if job.session and job.kind in ("post", "segments", "ai_scan", "thumbs"):
                    self._pending_add("pending_post", job.session)
                elif job.kind in ("ai_diagnose", "match"):
                    self._pending_add("pending_diag" if job.kind == "ai_diagnose" else "pending_match",
                                      job.key.rsplit("|", 1)[-1])
        if self.cfg.auto_optimize:
            threading.Thread(target=self._power_for_game, daemon=True).start()

    def _power_for_game(self) -> None:
        try:
            ok, msg = self.opt.apply_power(session=True)
            log.info("赛前优化（电源计划）：%s", msg)
        except Exception:
            log.exception("切换电源计划出错")
        self._opt_cache = (0.0, [])

    def _on_session_closed(self, name: str) -> None:
        log.info("场次结束：%s", name)
        self._sessions_cache = (0.0, [])
        if self.cfg.post_auto_analyze:
            self._pending_add("pending_post", name)

    def _on_game_exit(self) -> None:
        log.info("游戏已关闭")
        if self.cfg.restore_power_after_game:
            def restore():
                msg = self.opt.restore_power(only_session=True)
                if msg:
                    log.info("已改回打游戏前的电源计划")
                self._opt_cache = (0.0, [])
            threading.Thread(target=restore, daemon=True).start()
        self._cleanup_old()
        self._resume_pending()

    def _pending_add(self, key: str, value: str) -> None:
        items = list(self.db.kv_get(key, []) or [])
        if value not in items:
            items.append(value)
            self.db.kv_set(key, items)

    def _pending_remove(self, key: str, value: str) -> None:
        items = [x for x in (self.db.kv_get(key, []) or []) if x != value]
        self.db.kv_set(key, items)

    def _resume_pending(self) -> None:
        if self.game.running and not self.cfg.live_analyze:
            return
        for name in list(self.db.kv_get("pending_post", []) or []):
            self._start_post(name)
        if not self.ai_ready()[0]:  # AI 关掉了或密钥删了：不再自动诊断
            self.db.kv_set("pending_diag", [])
            self.db.kv_set("pending_match", [])
            return
        for match_id in list(self.db.kv_get("pending_match", []) or []):
            try:
                self._start_match_job(int(match_id))
            except (UserError, ValueError):
                self._pending_remove("pending_match", match_id)
        for event_id in list(self.db.kv_get("pending_diag", []) or []):
            try:
                self._start_diagnose(int(event_id))
            except (UserError, ValueError):
                self._pending_remove("pending_diag", event_id)

    def _cleanup_old(self) -> None:
        try:
            removed = sessions.cleanup(self.cfg.recordings_path(), self.cfg.retention_days,
                                       active=self.controller.session_dir)
            if removed:
                self._sessions_cache = (0.0, [])
        except OSError as exc:
            log.warning("清理旧录像失败：%s", exc)

    def _save_cfg(self) -> None:
        with self._cfg_lock:
            config.save(self.cfg)

    # =================================================================
    # 调用入口
    # =================================================================
    def call(self, method: str, args: dict | None = None) -> dict:
        fn = getattr(self, "m_" + method, None) if method.isidentifier() else None
        if fn is None:
            return {"ok": False, "error": f"未知操作：{method}"}
        args = args or {}
        try:
            inspect.signature(fn).bind(**args)
        except TypeError:
            log.warning("参数不对：%s %s", method, list(args))
            return {"ok": False, "error": "参数不对，请刷新界面后重试"}
        try:
            return {"ok": True, "data": fn(**args)}
        except (UserError, JobError, AIError, media.MediaError, perfmon.MeasureError) as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            log.exception("处理 %s 出错", method)
            return {"ok": False, "error": f"出错了：{exc}"}

    # =================================================================
    # 状态（每秒一次，必须快）
    # =================================================================
    def m_state(self) -> dict:
        g, w, rec = self.game, self.game.window, self.controller
        live = rec.live_stats() if rec.recording else {}
        seg_len = max(1, int(self.cfg.segment_seconds))
        in_seg = _parse_clock(live.get("time", "")) % seg_len if live else 0.0
        return {
            "version": __version__, "build": BUILD, "first_run": not self.cfg.first_run_done,
            "auto_record": self.cfg.auto_record,
            "game": {"running": g.running, "foreground": g.foreground,
                     "exe": (w.exe if w else self.game_exe) if g.running else "",
                     "title": w.title if w else "", "width": w.rect.width if w else 0, "height": w.rect.height if w else 0},
            "recorder": {"status": self.encoder_failed if self.encoder_failed and not rec.recording else rec.status,
                         "recording": rec.recording, "encoder": rec.encoder,
                         "encoder_busy": self.encoder_busy, "fps": live.get("fps", ""), "drop": live.get("drop", "0"),
                         "seg_time": _fmt_clock(in_seg) if live else "", "seg_progress": in_seg / seg_len,
                         "capture": "DDA 桌面复制 · 裁剪到游戏窗口", "ffmpeg_ok": bool(rec.ffmpeg), "error": rec.error,
                         "output_changes": rec.output_changes, "failures": rec.failures},
            "storage": self._storage_info(),
            "gpu": self.gpu.read(),
            "ai": {"configured": bool(self._api_key()), "text_model": self.cfg.ai_text_model,
                   "vision_model": self.cfg.ai_vision_model, "enabled": self.cfg.ai_vision_enabled,
                   "provider": self._provider()["label"],
                   "vision_ok": self.db.kv_get("vision_ok", "") == self._vision_key(),
                   "error": self._ai_error if time.time() < self._ai_pause_until else ""},
            "jobs": self.jobs.list(),
        }

    def _storage_info(self) -> dict:
        at, info = self._storage
        if time.time() - at < 30:
            return info
        root = self.cfg.recordings_path()
        used = 0
        if root.exists():
            for p in root.rglob("*"):
                try:
                    if p.is_file():
                        used += p.stat().st_size
                except OSError:
                    continue
        probe = root
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            free = shutil.disk_usage(probe).free
        except OSError:
            free = 0
        info = {"used_bytes": used, "free_bytes": free, "path": str(root)}
        self._storage = (time.time(), info)
        return info

    # =================================================================
    # 场次、局与事件
    #
    # 存储单位是“场次”：一次打开游戏到关闭游戏，对应录像文件夹里的一组分段。
    # 界面上的单位是“局”：AI 看出一局的开始和结束后，一个场次会拆成几局，名字是“场次名#局编号”。
    # 还没切分出局的场次（没开 AI、或者正在分析）整场显示为一条。
    # 事件和片段都用整场时间存储；界面上看到的时间是这一局内的时间（减去这一局的开始时间）。
    # =================================================================
    def _build_stores(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        active = self.controller.session.name if self.controller.session else None
        for s in sessions.list_sessions(self.cfg.recordings_path()):
            files = media.list_video_files(s.folder)
            data = sessions.read_session(s.folder) or {}
            if not files and not data:
                continue
            out[s.name] = {"name": s.name, "folder": str(s.folder), "started_at": s.started_at,
                           "duration": self._duration_hint(s.name, data), "size": s.size_bytes, "imported": False,
                           "source": "", "files": files, "active": s.name == active, "gone": not files,
                           "runs": data.get("runs") or []}
        metas = {m["name"]: m for m in self.db.query("SELECT * FROM session_meta")}
        for m in metas.values():
            if not m.get("imported") or m["name"] in out:
                continue
            p = Path(m.get("source_path") or "")
            exists = p.is_file()
            out[m["name"]] = {"name": m["name"], "folder": str(p.parent), "started_at": m.get("started_at") or
                              _name_time(m["name"]), "duration": m.get("duration") or 0.0,
                              "size": p.stat().st_size if exists else 0, "imported": True, "source": str(p),
                              "files": [p] if exists else [], "active": False, "gone": not exists, "runs": []}
        for row in self.db.query("SELECT DISTINCT session FROM events UNION SELECT DISTINCT session FROM clips"):
            name = row["session"]
            if not name or name in out:
                continue
            m = metas.get(name, {})
            clips_dir = paths.data_dir() / "clips" / name
            size = sum(f.stat().st_size for f in clips_dir.glob("*.webm")) if clips_dir.exists() else 0
            out[name] = {"name": name, "folder": str(clips_dir), "started_at": m.get("started_at") or _name_time(name),
                         "duration": m.get("duration") or 0.0, "size": size, "imported": bool(m.get("imported")),
                         "source": m.get("source_path") or "", "files": [], "active": False, "gone": True, "runs": []}
        return out

    def _sessions(self, fresh: bool = False) -> list[dict]:
        """界面上的列表：每一局一条（还没切分的场次整场一条），按开始时间倒序。"""
        with self._sessions_lock:
            at, cached = self._sessions_cache
            if not fresh and time.time() - at < 3:
                return cached
            stores = self._build_stores()
            by_session: dict[str, list] = {}
            for m in self.db.matches():
                by_session.setdefault(m["session"], []).append(m)
            views = []
            for st in stores.values():
                rows = by_session.get(st["name"], [])
                if not rows:
                    views.append({**st, "session": st["name"], "match_id": None, "ordinal": 0, "t0": 0.0, "t1": None,
                                  "open": False, "auto": {}})
                    continue
                wall = self._wall_fn(st)
                for k, m in enumerate(rows, 1):
                    t0, t1 = float(m["t_start"]), float(m["t_end"])
                    views.append({**st, "name": f"{st['name']}#{m['id']}", "session": st["name"], "match_id": m["id"],
                                  "ordinal": k, "t0": t0, "t1": t1 if m["closed"] else None,
                                  "started_at": wall(t0), "duration": max(0.0, t1 - t0),
                                  "active": st["active"] and not m["closed"], "open": not m["closed"],
                                  "auto": {"map": m["map"] or "", "mode": m["mode"] or "", "result": m["result"] or ""}})
            views.sort(key=lambda v: v["started_at"] or 0, reverse=True)
            self._sessions_cache = (time.time(), views)
            return views

    def _wall_fn(self, st: dict):
        """录像时间 → 现实时间。切到后台时录像暂停，所以要按每段录制（run）的开始时间换算。"""
        base = float(st["started_at"] or 0)
        idx_path = paths.data_dir() / "media" / st["name"] / media.INDEX_FILE
        try:
            index = json.loads(idx_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            index = []
        run_start = {int(r.get("index") or 0): float(r.get("started_at") or 0) for r in st.get("runs") or []}
        first: dict[int, float] = {}
        items = []
        for item in index if isinstance(index, list) else []:
            m = media._SEGMENT_RE.search(Path(str(item.get("path", ""))).name)
            if not m:
                continue
            run = int(m.group(1))
            first.setdefault(run, float(item.get("start") or 0))
            items.append((float(item.get("start") or 0), run))

        def wall(t: float) -> float:
            for start, run in reversed(items):
                if t >= start and run in run_start:
                    return run_start[run] + (t - first[run])
            return base + t
        return wall

    def _duration_hint(self, name: str, data: dict) -> float:
        idx = paths.data_dir() / "media" / name / media.INDEX_FILE
        try:
            return float(sum(float(x.get("duration") or 0) for x in json.loads(idx.read_text(encoding="utf-8"))))
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        total = 0.0
        for run in data.get("runs") or []:
            end = run.get("ended_at") or (time.time() if not data.get("ended_at") else 0)
            if end and run.get("started_at"):
                total += max(0.0, end - run["started_at"])
        return total

    def _store(self, session: str) -> dict | None:
        return next((v for v in self._sessions() if v["session"] == session), None)

    def _view(self, name: str) -> dict:
        v = next((x for x in self._sessions() if x["name"] == name), None)
        if v:
            return v
        session, _, mid = name.partition("#")
        st = self._store(session)
        if st and not mid:  # 已经切成几局的场次，也可以按整场打开
            return {**st, "name": session, "session": session, "match_id": None, "ordinal": 0, "t0": 0.0, "t1": None,
                    "open": False, "auto": {}}
        raise UserError("没有找到这场录像，可能已被清理")

    # 兼容旧的调用：按名字找场次（找不到返回 None）
    def _session(self, name: str) -> dict | None:
        try:
            return self._view(name)
        except UserError:
            return None

    @staticmethod
    def _in_view(v: dict, t: float) -> bool:
        end = v["t1"] if v.get("t1") is not None else float("inf")
        return v.get("t0", 0.0) - 0.5 <= float(t) < end + 0.5

    def _is_recording_session(self, session: str) -> bool:
        return bool(self.controller.session and self.controller.session.name == session)

    def _completed_files(self, session: str) -> list[Path]:
        """这个场次里已经录完的分段（正在写的那一段不算）。"""
        if self._is_recording_session(session) and self.controller.session_dir is not None:
            files = media.list_video_files(self.controller.session_dir)
        else:
            st = self._store(session)
            if not st:
                return []
            files = st["files"] if st["imported"] else media.list_video_files(Path(st["folder"]))
        folder, prefix = self.controller.current_files()
        if prefix and folder is not None and self._is_recording_session(session):
            writing = [f for f in files if f.name.startswith(prefix)]
            if writing:
                newest = max(writing, key=lambda f: f.name)
                files = [f for f in files if f != newest]
        return files

    def _timeline(self, session: str) -> media.Timeline:
        return media.Timeline(session, self._completed_files(session))

    def _all_events(self) -> list[dict]:
        """所有事件，附上所属的局（view）和现实时间（at）。"""
        by_session: dict[str, list] = {}
        for v in self._sessions():
            by_session.setdefault(v["session"], []).append(v)
        events = self.db.events()
        for e in events:
            vs = by_session.get(e["session"], [])
            v = next((x for x in vs if self._in_view(x, e["t"])), None)
            e["view"] = v["name"] if v else None
            if v:
                e["at"] = float(v["started_at"] or 0) + float(e["t"]) - v["t0"]
            else:
                e["at"] = float(_name_time(e["session"]) or e.get("created") or 0) + float(e["t"])
        return events

    def _sessions_since(self, ts) -> int:
        ts = float(ts or 0)
        return sum(1 for s in self._sessions() if (s["started_at"] or 0) >= ts)

    def _event_out(self, e: dict, t0: float = 0.0) -> dict:
        clip = None
        if e.get("clip_path") and Path(e["clip_path"]).exists():
            clip = {"src": media.file_url(e["clip_path"]), "t_start": float(e["clip_start"]) - t0,
                    "t_end": float(e["clip_end"]) - t0}
        ai = None
        if e.get("ai_json"):
            try:
                ai = json.loads(e["ai_json"])
            except ValueError:
                ai = None
        return {"id": e["id"], "kind": e["kind"], "t": float(e["t"]) - t0, "cause": e.get("cause") or "",
                "source": e.get("source"), "confirmed": bool(e.get("confirmed")),
                "killer_weapon": e.get("killer_weapon") or "", "distance_m": e.get("distance_m"), "clip": clip, "ai": ai}

    def _meta(self, v: dict) -> dict:
        """这一局的模式 / 地图 / 结果：你手动标的优先，没标就用 AI 读到的。"""
        user = self.db.meta(v["name"])
        auto = v.get("auto") or {}
        return {k: (user.get(k) or auto.get(k) or "") for k in ("mode", "map", "result")}

    def _title(self, s: dict, meta: dict) -> str:
        mode = meta.get("mode") or ""
        title = " · ".join(p for p in (meta.get("map") or "", MODES.get(mode, "") if mode else "") if p)
        if not title:
            if s["imported"] and s["source"]:
                title = Path(s["source"]).stem[:28]
            else:
                title = f"三角洲行动 · 第 {s['ordinal']} 局" if s.get("ordinal") else "三角洲行动对局"
        if s.get("open"):
            title += " · 进行中"
        elif s.get("active"):
            title += " · 录制中"
        elif s.get("gone"):
            title += " · 原片已清理（片段保留）"
        return title

    @staticmethod
    def _label(v: dict) -> str:
        return f"{v['session']} · 第 {v['ordinal']} 局" if v.get("ordinal") else v["session"]

    def _retention_label(self) -> str:
        return f"{self.cfg.retention_days} 天" if self.cfg.retention_days else "永久"

    def m_matches(self, mode: str = "", map: str = "", result: str = "", days: int = 7) -> dict:  # noqa: A002
        now = time.time()
        by_view: dict[str, list] = {}
        for e in self._all_events():
            if e["view"]:
                by_view.setdefault(e["view"], []).append(e)
        rows, picked, maps = [], [], set()
        for s in self._sessions():
            meta = self._meta(s)
            if meta["map"]:
                maps.add(meta["map"])
            if days and (s["started_at"] or 0) < now - float(days) * 86400:
                continue
            if (mode and meta["mode"] != mode) or (map and meta["map"] != map) or (result and meta["result"] != result):
                continue
            evs = by_view.get(s["name"], [])
            picked += evs
            w, l = stats.duel_counts(evs)
            deaths = [e for e in evs if e["kind"] == "death"]
            rows.append({"name": s["name"], "title": self._title(s, meta), "started_at": s["started_at"],
                         "duration": s["duration"], "death_count": len(deaths), "duel": {"w": w, "l": l},
                         "size": s["size"], "result": meta["result"], "mode": meta["mode"],
                         "imported": s["imported"], "deaths": [self._event_out(e, s["t0"]) for e in deaths]})
        duel = stats.duel_summary(picked)
        cause, share, _k, _n = stats.top_cause(picked)
        return {"list": rows, "maps": sorted(maps), "retention_label": self._retention_label(),
                "kpis": {"sessions": len(rows), "deaths": sum(1 for e in picked if stats.counted_death(e)),
                         "wins": duel["wins"], "losses": duel["losses"], "duel_total": duel["total"],
                         "duel_rate": duel["rate"], "top_cause": cause, "top_cause_share": share,
                         "unconfirmed": sum(1 for e in picked if stats.unconfirmed_ai(e))}}

    def _spec(self, s: dict, tl: media.Timeline) -> str:
        w, h = tl.resolution
        fps = next((seg.fps for seg in tl.segments if seg.fps), 0) or (self.cfg.framerate if not s["imported"] else 0)
        if s["runs"]:
            crop = (s["runs"][-1] or {}).get("crop") or {}
            w, h = crop.get("width") or w, crop.get("height") or h
            enc = (s["runs"][-1] or {}).get("encoder") or ""
            return f"{w}×{h} · {fps:.0f}FPS · {enc.upper()}" if w else "—"
        if not w:
            return "原片已清理" if s.get("gone") else "—"
        return f"{w}×{h} · {fps:.0f}FPS" + (" · 导入" if s["imported"] else "")

    def m_session_detail(self, name: str) -> dict:
        v = self._view(name)
        session = v["session"]
        tl = self._timeline(session)
        if v["imported"] and tl.segments and abs(tl.duration - (v["duration"] or 0)) > 1:
            self.db.set_meta(session, duration=tl.duration, started_at=v["started_at"])
        t0 = v["t0"]
        t1 = v["t1"] if v["t1"] is not None else tl.duration
        thumbs, step = tl.thumbs()
        thumbs = [th for th in thumbs if t0 - 0.5 <= th["t"] < t1]
        pending = any(self.jobs.active(k, session) for k in ("segments", "post", "thumbs"))
        if not thumbs and tl.segments and not pending and (self.cfg.live_analyze or not self.game.running):
            self._start_segments(session, final=not self._is_recording_session(session))
            pending = True
        evs = [e for e in self.db.events(session) if self._in_view(v, e["t"])]
        last = None
        for clip in self.db.query("SELECT * FROM clips WHERE session=? ORDER BY id DESC", (session,)):
            if t0 - 15 <= clip["t_start"] < t1 and Path(clip["path"]).exists():
                last = {"src": media.file_url(clip["path"]), "t_start": clip["t_start"] - t0, "t_end": clip["t_end"] - t0}
                break
        return {"name": v["name"], "label": self._label(v), "folder": v["folder"], "spec": self._spec(v, tl),
                "match_id": v.get("match_id"), "open": bool(v.get("open")),
                "total_duration": max(0.0, t1 - t0),
                "thumbs": [{"t": round(th["t"] - t0, 2), "src": media.file_url(th["path"])} for th in thumbs],
                "thumb_step": step, "thumbs_pending": bool(pending),
                "events": [self._event_out(e, t0) for e in evs],
                "ai_ready": self.ai_ready()[0], "last_clip": last, "meta": self._meta(v),
                "match_advice": self._match_advice(v, evs)}

    def m_set_session_meta(self, name: str, mode: str | None = None, map: str | None = None,  # noqa: A002
                           result: str | None = None) -> dict:
        fields = {}
        if mode is not None:
            if mode not in MODES:
                raise UserError("模式不对")
            fields["mode"] = mode
        if map is not None:
            fields["map"] = str(map).strip()[:20]
        if result is not None:
            if result not in RESULTS:
                raise UserError("结果不对")
            fields["result"] = result
        s = self._session(name)
        if s:
            fields.setdefault("started_at", s["started_at"])
        self.db.set_meta(name, **fields)
        self._sessions_cache = (0.0, [])
        return {}

    def m_add_event(self, name: str, t: float, kind: str) -> dict:
        if kind not in EVENT_KINDS:
            raise UserError("标记类型不对")
        v = self._view(name)
        t = max(0.0, float(t)) + v["t0"]
        event_id = self.db.add_event(v["session"], t, kind, source="manual")
        if kind == "death":
            clip = self._covering_clip(v["session"], t)
            if clip:
                self.db.update_event(event_id, clip_path=clip[0], clip_start=clip[1], clip_end=clip[2])
        self._refresh_recs()
        return {"id": event_id}

    def _covering_clip(self, name: str, t: float) -> tuple | None:
        """正在看的片段如果覆盖了这个时间点，就直接挂到新标记上，回放不中断。"""
        best = None
        rows = [(r["path"], r["t_start"], r["t_end"]) for r in self.db.query(
            "SELECT path, t_start, t_end FROM clips WHERE session=?", (name,))]
        rows += [(r["clip_path"], r["clip_start"], r["clip_end"]) for r in self.db.query(
            "SELECT clip_path, clip_start, clip_end FROM events WHERE session=? AND clip_path IS NOT NULL", (name,))]
        for path, a, b in rows:
            if path and a is not None and b is not None and a <= t <= b and Path(path).exists():
                if best is None or t - a > t - best[1]:
                    best = (path, a, b)
        return best

    def m_update_event(self, id: int, cause: str | None = None, confirmed: bool | None = None) -> dict:  # noqa: A002
        fields: dict = {}
        if cause is not None:
            if cause and cause not in stats.CAUSES:
                raise UserError("死因不对")
            fields["cause"] = cause
        if confirmed is not None:
            fields["confirmed"] = int(bool(confirmed))
        if not self.db.event(int(id)):
            raise UserError("这个标记已经不存在了")
        self.db.update_event(int(id), **fields)
        self._refresh_recs()
        return {}

    def m_delete_event(self, id: int) -> dict:  # noqa: A002
        ev = self.db.event(int(id))
        if ev:
            self.db.delete_event(int(id))
            path = ev.get("clip_path")
            if path and not self.db.one("SELECT 1 AS x FROM events WHERE clip_path=? UNION SELECT 1 FROM clips WHERE path=?",
                                        (path, path)):
                Path(path).unlink(missing_ok=True)
            self._refresh_recs()
        return {}

    def m_import_video(self, path: str = "") -> dict:
        if not path:
            return {}
        p = Path(path)
        if not p.is_file() or p.suffix.lower() not in media.VIDEO_EXTS:
            raise UserError("请选择 mp4 / mkv / mov 等视频文件")
        existing = self.db.one("SELECT name FROM session_meta WHERE imported=1 AND source_path=?", (str(p),))
        if existing:
            return {"name": existing["name"]}
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(p.stat().st_mtime))
        name = f"import_{stamp}"
        k = 1
        while self.db.one("SELECT 1 AS x FROM session_meta WHERE name=?", (name,)):
            k += 1
            name = f"import_{stamp}_{k}"
        self.db.set_meta(name, source_path=str(p), imported=1, started_at=p.stat().st_mtime)
        self._sessions_cache = (0.0, [])
        self._start_post(name)
        return {"name": name}

    # ---- 片段、缩略图 ----
    def m_make_clip(self, name: str, t: float, event_id: int | None = None) -> dict:
        if event_id:
            job = self._start_clip_for_event(int(event_id))
        else:
            v = self._view(name)
            session, ts = v["session"], float(t) + v["t0"]

            def work(job):
                clip = self._timeline(session).make_clip(ts)
                self.db.execute("INSERT INTO clips(session,path,t_start,t_end,created) VALUES(?,?,?,?,?)",
                                (session, clip["path"], clip["t_start"], clip["t_end"], time.time()))
                return "片段已生成，可以逐帧回放了"
            job = self.jobs.start("clip", "生成片段", work, session=session, key=f"t{ts:.0f}")
        return {"job": job.id}

    def m_export_match(self, name: str) -> dict:
        """把这一局保存成一个完整的录像文件（放在录像文件夹的“单局录像”里，不会被自动清理）。"""
        v = self._view(name)
        if v.get("open"):
            raise UserError("这一局还没打完，打完再保存")
        session = v["session"]
        t0 = float(v.get("t0") or 0.0)
        t1 = float(v["t1"]) if v.get("t1") is not None else float("inf")
        title = self._title(v, self._meta(v)).replace(" · ", "_")
        stamp = time.strftime("%Y%m%d_%H%M", time.localtime(v.get("started_at") or time.time()))
        safe = "".join(ch for ch in f"{stamp}_{title}" if ch not in '\\/:*?"<>|').strip() or stamp
        out = self.cfg.recordings_path() / sessions.EXPORT_DIR / f"{safe}.mkv"

        def work(job):
            tl = self._timeline(session)
            try:
                path = tl.export_range(t0, min(t1, tl.duration), out)
            except media.MediaError as exc:
                raise JobError(str(exc)) from exc
            return f"这一局的完整录像已保存：{sessions.EXPORT_DIR}\\{path.name}（不会被自动清理）"
        job = self.jobs.start("export", "保存这一局的完整录像", work, session=session, key=name)
        return {"job": job.id, "folder": str(out.parent)}

    def _start_clip_for_event(self, event_id: int):
        ev = self.db.event(event_id)
        if not ev:
            raise UserError("这个标记已经不存在了")

        def work(job):
            self._clip_for_event(event_id)
            return "阵亡片段已生成"
        return self.jobs.start("clip", "生成阵亡片段", work, session=ev["session"], key=f"e{event_id}")

    def _clip_for_event(self, event_id: int) -> None:
        ev = self.db.event(event_id)
        if not ev or (ev.get("clip_path") and Path(ev["clip_path"]).exists()):
            return
        clip = self._timeline(ev["session"]).make_clip(float(ev["t"]))
        self.db.update_event(event_id, clip_path=clip["path"], clip_start=clip["t_start"], clip_end=clip["t_end"])

    def m_make_thumbs(self, name: str) -> dict:
        v = self._view(name)
        if self.game.running and not self.cfg.live_analyze:
            raise UserError("游戏运行中不做录像处理，关掉游戏后会自动生成")
        return {"job": self._start_segments(v["session"], final=not self._is_recording_session(v["session"])).id}

    # =================================================================
    # AI：看图模型自检、边录边分析、按局找阵亡
    # =================================================================
    def _provider(self) -> dict:
        return {"id": self.cfg.ai_provider if self.cfg.ai_provider in PROVIDERS else "deepseek",
                **PROVIDERS.get(self.cfg.ai_provider, PROVIDERS["deepseek"])}

    def _base_url(self) -> str:
        return (self.cfg.ai_base_url or self._provider()["base_url"]).rstrip("/")

    def _client(self, timeout: float = 60) -> AIClient:
        return AIClient(self._api_key(), self._base_url(), provider=self._provider()["id"], timeout=timeout)

    def _vision_key(self) -> str:
        """看图自检的结果按“平台 + 地址 + 模型”记，换了任何一个都要重新检查。"""
        return f"{self._provider()['id']}|{self._base_url()}|{self.cfg.ai_vision_model}"

    def _api_key(self) -> str:
        provider = self._provider()["id"]
        if self._key is None or self._key_provider != provider:
            self._key = secrets_store.get_api_key(provider)
            self._key_provider = provider
        return self._key

    def ai_ready(self) -> tuple[bool, str]:
        if not self.cfg.ai_vision_enabled:
            return False, "AI 看图没有开启（设置 → 隐私与数据）"
        if not self._api_key():
            return False, f"还没有填写 {self._provider()['label']} 的 API Key"
        if not self.cfg.ai_vision_model:
            return False, "还没有选择看图模型（设置 → AI 模型接入 → 测试连接）"
        if self.db.kv_get("vision_ok", "") != self._vision_key():
            fail = self.db.kv_get("vision_fail", {}) or {}
            if fail.get("key") == self._vision_key() and time.time() - float(fail.get("at") or 0) < VISION_RETRY_SEC:
                return False, fail.get("message") or "看图模型自检没有通过"
            self._start_vision_check()
            return False, "正在检查看图模型能不能真的看到图片…"
        return True, ""

    def _ai_on(self) -> bool:
        return self.ai_ready()[0] and time.time() >= self._ai_pause_until

    def _pause_ai(self, exc: Exception) -> None:
        self._ai_pause_until = time.time() + AI_PAUSE_SEC
        self._ai_error = str(exc)
        log.warning("AI 暂停 %d 分钟：%s", AI_PAUSE_SEC // 60, exc)

    def _start_vision_check(self):
        def work(job):
            current = self.cfg.ai_vision_model
            key_before = self._vision_key()
            last = ""
            with self._client(timeout=60) as client:
                models = self.db.kv_get("ai_models", []) or []
                if not models:
                    try:
                        models = client.list_models()
                        self.db.kv_set("ai_models", models)
                    except AIError:
                        models = []
                candidates = [current] + [m for m in self._provider()["vision"] if m != current and (m in models or not models)]
                for model in dict.fromkeys(c for c in candidates if c):
                    ok, msg, r = aiclient.vision_self_test(client, model)
                    if r:
                        self.db.add_usage(r.model, "vision_test", r.prompt_tokens, r.completion_tokens)
                    log.info("看图自检：%s", msg)
                    if ok:
                        with self._cfg_lock:
                            self.cfg.ai_vision_model = model
                            self._save_cfg()
                        self.db.kv_set("vision_ok", self._vision_key())
                        self.db.kv_set("vision_fail", {})
                        self._sessions_cache = (0.0, [])
                        return msg + (f"。原来选的 {current} 看不到图片，已自动换成 {model}" if model != current else "")
                    last = msg
            self.db.kv_set("vision_fail", {"key": key_before, "message": last, "at": time.time()})
            raise JobError(last or "看图模型自检没有通过")
        return self.jobs.start("vision", "检查看图模型", work, pool="net")

    def _ai_session(self, job) -> analysis.AISession:
        ok, why = self.ai_ready()
        if not ok:
            raise JobError(why)
        return analysis.AISession(self._api_key(), self._base_url(), self.cfg.ai_vision_model, self.db,
                                  self.cfg.ai_daily_token_limit, provider=self._provider()["id"],
                                  cancelled=lambda: job.cancelled or (self.game.running and not self.cfg.live_analyze))

    def _start_post(self, name: str):
        """游戏关闭（或导入录像）后：把剩下的分段处理完，结束最后一局。"""
        return self._start_segments(name, final=True)

    def _start_segments(self, session: str, final: bool, notify: bool | None = None):
        def work(job):
            return self._job_segments(job, session, final)
        return self.jobs.start("segments", "整理录像" if final else "边录边分析", work, session=session,
                               key="final" if final else "", pool="ai",
                               notify=final if notify is None else notify)

    def _job_segments(self, job, session: str, final: bool) -> str:
        """处理录完的分段：缩略图 → 本机文字识别（大厅 / 结算 / 阵亡）→ AI 看图（开了的话）→ 合并成画面状态 → 切局。"""
        files = self._completed_files(session)
        tl = media.Timeline(session, files)
        if not tl.segments:
            self._pending_remove("pending_post", session)
            return "没有找到录像文件"
        ai_on = self._ai_on()
        pending = [seg for seg in tl.segments if not self.db.seg_is_done(session, tl._key(seg), need_ai=ai_on)]
        ai = self._ai_session(job) if ai_on and pending else None
        note = ""
        try:
            for i, seg in enumerate(pending):
                job.check()
                job.label = f"整理录像：第 {i + 1}/{len(pending)} 段"
                tl.thumbs_for(seg, self.cfg.thumb_step)
                ocr_states: dict[float, tuple[str, dict]] = {}
                if ocr.available():
                    for t, img in tl.frames_for(seg, analysis.SCAN_EVERY):
                        ocr_states[t] = ocr.classify(ocr.recognize(img))
                        if not ocr.available():
                            ocr_states = {}
                            break
                ai_states: dict[float, tuple[str, dict]] = {}
                did_ai = False
                if ai is not None and time.time() >= self._ai_pause_until:
                    try:
                        for sheet in tl.sheets_for(seg, analysis.SCAN_EVERY, analysis.SCAN_COLS, analysis.SCAN_ROWS, 480):
                            if ocr_states and all(ocr_states.get(t, ("",))[0] == "lobby" for t in sheet["times"]):
                                continue  # 大厅画面不用再发给 AI，省 token
                            res = analysis.classify_sheet(ai, sheet)
                            info = {k: res[k] for k in ("map", "mode", "result") if res.get(k)}
                            for t, st in zip(sheet["times"], res["states"]):
                                ai_states[t] = (st, info)
                        did_ai = True
                    except analysis.Cancelled:
                        raise JobCancelled()
                    except analysis.BudgetExceeded as exc:
                        self._pause_ai(exc)
                        note = str(exc)
                        today = time.strftime("%Y-%m-%d")
                        job.notify = self.db.kv_get("budget_notified", "") != today
                        self.db.kv_set("budget_notified", today)
                    except AIError as exc:
                        self._pause_ai(exc)
                        note = f"AI 暂时不可用：{exc}"
                rows = [(t, *_merge_state(ocr_states.get(t), ai_states.get(t)))
                        for t in sorted(set(ocr_states) | set(ai_states))]
                if rows:
                    self.db.add_states(session, rows)
                self.db.mark_seg(session, tl._key(seg), did_ai)
                job.set_progress((i + 1) / len(pending))
        finally:
            if ai is not None:
                ai.close()
        new_matches = self._update_matches(session, final)
        if final:
            self._pending_remove("pending_post", session)
        self._sessions_cache = (0.0, [])
        if note:
            return f"录像已整理好。{note}"
        if new_matches:
            return f"识别出 {new_matches} 局，正在逐局分析"
        if not ai_on and not ocr.available():
            return "录像已整理好（缩略图）。开启 AI 看图后，会自动按局切分、找阵亡、判断死因"
        return "录像已整理好"

    def _update_matches(self, session: str, final: bool) -> int:
        """根据画面状态重新切分这一场；新结束的局排队做阵亡分析。返回新结束的局数。"""
        frames = self.db.states(session)
        if not frames:
            return 0
        if not any(f.get("src") in ("ai", "mix") for f in frames):
            # 只有本机文字识别时：第一次看到大厅之前（启动画面、登录、公告）都不算对局
            first_lobby = next((i for i, f in enumerate(frames) if f["state"] == "lobby"), len(frames))
            for f in frames[:first_lobby]:
                f["state"] = "loading"
        detected = matchlib.detect(frames, step=analysis.SCAN_EVERY, final=final)
        existing = self.db.matches(session)
        used: set[int] = set()
        newly_closed = []
        for d in detected:
            row = None
            best = 0.0
            for r in existing:
                if r["id"] in used:
                    continue
                overlap = min(d.t_end, r["t_end"]) - max(d.t_start, r["t_start"])
                if overlap > best:
                    row, best = r, overlap
            deaths = json.dumps(d.deaths)
            if row is not None:
                used.add(row["id"])
                if row["closed"] and row["analyzed_at"]:
                    continue  # 已经分析过的局不再变
                self.db.execute("UPDATE matches SET t_start=?, t_end=?, closed=?, map=?, mode=?, result=?, deaths_json=? "
                                "WHERE id=?", (d.t_start, d.t_end, int(d.closed), d.map, d.mode, d.result, deaths, row["id"]))
                if d.closed and not row["closed"]:
                    newly_closed.append(row["id"])
            else:
                cur = self.db.execute("INSERT INTO matches(session,t_start,t_end,closed,map,mode,result,deaths_json,created) "
                                      "VALUES(?,?,?,?,?,?,?,?,?)", (session, d.t_start, d.t_end, int(d.closed), d.map,
                                                                   d.mode, d.result, deaths, time.time()))
                if d.closed:
                    newly_closed.append(int(cur.lastrowid))
        for r in existing:  # 没分析过、这次也没再识别出来的局去掉（多半是之前的误判）
            if r["id"] not in used and not r["analyzed_at"]:
                self.db.execute("DELETE FROM matches WHERE id=?", (r["id"],))
        self._sessions_cache = (0.0, [])
        for match_id in newly_closed:
            self._start_match_job(match_id)
        return len(newly_closed)

    def _start_match_job(self, match_id: int):
        m = self.db.match(match_id)
        if not m:
            raise UserError("这一局已经不存在了")

        def work(job):
            return self._job_match(job, match_id)
        return self.jobs.start("match", "分析这一局", work, session=m["session"], key=str(match_id), pool="ai")

    def _job_match(self, job, match_id: int) -> str:
        m = self.db.match(match_id)
        if not m:
            return "这一局已经不存在了"
        session = m["session"]
        tl = self._timeline(session)
        # 没开 AI 时也照样处理：阵亡时间用文字识别找到的，生成片段，死因留给你点
        ai = self._ai_session(job) if self._ai_on() else None
        added: list[tuple[int, float, str]] = []
        try:
            deaths = json.loads(m["deaths_json"] or "[]")
            known = [float(e["t"]) for e in self.db.events(session)
                     if e["kind"] == "death" and m["t_start"] - 5 <= float(e["t"]) <= m["t_end"] + 5]
            for i, (t, kind) in enumerate(deaths):
                job.check()
                job.label = f"分析这一局：确认阵亡时间 {i + 1}/{len(deaths)}"
                if any(abs(float(t) - x) <= analysis.DEDUP_SEC for x in known + [a[1] for a in added]):
                    continue
                t_ref = analysis.refine(ai, tl, float(t)) if ai is not None else float(t)
                event_id = self.db.add_event(session, t_ref, "death", source="ai" if ai is not None else "auto")
                added.append((event_id, t_ref, kind))
                job.set_progress(0.3 * (i + 1) / max(1, len(deaths)))
            for k, (event_id, _t, kind) in enumerate(added):
                job.label = f"分析这一局：诊断阵亡 {k + 1}/{len(added)}"
                try:
                    self._clip_for_event(event_id)
                except media.MediaError as exc:
                    log.info("片段生成失败（继续诊断）：%s", exc)
                if ai is not None:
                    try:
                        self._diagnose(ai, event_id, knocked=kind == "down")
                    except analysis.BudgetExceeded:
                        raise
                    except AIError as exc:
                        log.info("诊断失败：%s", exc)
                job.set_progress(0.3 + 0.7 * (k + 1) / len(added))
        except analysis.Cancelled:
            raise JobCancelled()
        finally:
            if ai is not None:
                ai.close()
        self.db.execute("UPDATE matches SET analyzed_at=? WHERE id=?", (time.time(), match_id))
        try:
            coach = self._coach(match_id)
            if coach:
                self.db.execute("UPDATE matches SET coach=? WHERE id=?", (coach, match_id))
        except AIError as exc:
            log.info("教练点评没写成：%s", exc)
        self._pending_remove("pending_match", str(match_id))
        self._sessions_cache = (0.0, [])
        self._refresh_recs()
        rows = self.db.matches(session)
        ordinal = next((k for k, r in enumerate(rows, 1) if r["id"] == match_id), 0)
        result = RESULTS.get(m["result"] or "", "")
        n = len(json.loads(m["deaths_json"] or "[]"))
        tail = ("，死因已给出初步判断，请到“对局录像”里确认" if ai is not None else "，到“对局录像”里点一下死因就出本局方案")
        return (f"第 {ordinal} 局分析完成" + (f"：{result}" if result and result != "未标注" else "") +
                (f"，阵亡 {n} 次{tail}" if n else "，这一局没有发现阵亡"))

    COACH_PROMPT = (
        "你是《三角洲行动》（PC 端第一人称射击游戏）的复盘教练。下面是玩家刚打完的一局，数据由软件从录像里识别，"
        "可能有误差：\n{facts}\n"
        "请根据这些数据给出 3 条最重要、下一局就能照做的改进建议：具体到操作、站位或游戏设置项，每条不超过 45 个字。"
        "不要编造数据里没有的信息；数据不够时就给通用但具体的练法。只输出 3 行，每行以“- ”开头。")

    def _match_facts(self, match_id: int) -> tuple[str, list[dict]]:
        m = self.db.match(match_id)
        if not m:
            return "", []
        evs = [e for e in self.db.events(m["session"]) if m["t_start"] - 0.5 <= float(e["t"]) <= m["t_end"] + 0.5]
        deaths = [e for e in evs if e["kind"] == "death"]
        w, l = stats.duel_counts(evs)
        mins, secs = divmod(int(m["t_end"] - m["t_start"]), 60)
        mode = MODES.get(m["mode"], "") if m["mode"] else ""
        result = RESULTS.get(m["result"], "") if m["result"] else ""
        lines = [f"- 模式：{mode or '未知'}；地图：{m['map'] or '未知'}；结果：{result or '未知'}；时长：{mins} 分 {secs} 秒",
                 f"- 阵亡 {len(deaths)} 次；对枪 赢 {w} 次、输 {l} 次"]
        for k, e in enumerate(deaths, 1):
            ai = json.loads(e["ai_json"]) if e.get("ai_json") else {}
            t = int(float(e["t"]) - m["t_start"])
            cause = stats.CAUSES.get(e.get("cause") or "", "未判断")
            parts = [f"第 {t // 60}:{t % 60:02d} 阵亡，死因：{cause}（{'已确认' if e.get('confirmed') else 'AI 判断'}）"]
            for key, label in (("process", "交火"), ("gear", "装备"), ("awareness", "视野"), ("advice", "AI 建议")):
                if ai.get(key):
                    parts.append(f"{label}：{ai[key]}")
            if e.get("killer_weapon"):
                parts.append(f"对方武器：{e['killer_weapon']}")
            if e.get("distance_m"):
                parts.append(f"距离约 {float(e['distance_m']):.0f} 米")
            lines.append(f"  {k}. " + "；".join(parts))
        prof = {k: v for k, v in (self.cfg.profile or {}).items() if v is not None}
        if prof:
            names = {"mouse": "鼠标灵敏度", "vertical": "垂直", "horizontal": "水平", "ads_boost": "举枪加成",
                     "ads_vertical": "举枪瞄准垂直", "ads_horizontal": "举枪瞄准水平", "fov": "FOV", "dpi": "DPI"}
            lines.append("- 玩家灵敏度设置：" + "，".join(f"{names.get(k, k)} {v:g}" for k, v in prof.items()))
        return "\n".join(lines), deaths

    def _coach(self, match_id: int) -> str:
        """一局分析完，让文字模型根据这一局的数据写 3 条点评（没配文字模型、没超额度才写）。"""
        if not self._api_key() or not self.cfg.ai_text_model or not self.cfg.ai_vision_enabled:
            return ""
        if self.cfg.ai_daily_token_limit and self.db.usage_today() + 2000 > self.cfg.ai_daily_token_limit:
            return ""
        facts, _deaths = self._match_facts(match_id)
        if not facts:
            return ""
        with self._client(timeout=60) as client:
            r = client.chat(self.cfg.ai_text_model, [{"role": "user", "content": self.COACH_PROMPT.format(facts=facts)}],
                            max_tokens=600, temperature=0.3)
        self.db.add_usage(r.model, "coach", r.prompt_tokens, r.completion_tokens)
        lines = [ln.strip().lstrip("-·•*0123456789.、 ").strip() for ln in r.text.splitlines() if ln.strip()]
        return "\n".join(ln[:80] for ln in lines[:3] if ln)

    def _match_advice(self, v: dict, events: list[dict]) -> dict:
        """一局结束就给的方案：按这一局的阵亡和死因（AI 判断的也用，并标明待确认）。
        长期方案（≥5 次确认死因、采纳后复测）仍然单独计算。"""
        deaths = [e for e in events if e["kind"] == "death"]
        counts: dict[str, int] = {}
        for e in deaths:
            if e.get("cause") in stats.CAUSES and e["cause"] != "other":
                counts[e["cause"]] = counts.get(e["cause"], 0) + 1
        items = []
        for cause, k in sorted(counts.items(), key=lambda kv: -kv[1])[:2]:
            adv = recommend.advice_for(cause, self.cfg.profile)
            if not adv:
                continue
            confirmed = sum(1 for e in deaths if e.get("cause") == cause and e.get("confirmed"))
            src = "已确认" if confirmed == k else ("AI 判断，待你确认" if confirmed == 0 else f"其中 {confirmed} 次已确认")
            items.append({"id": f"m{v.get('match_id') or 0}-{cause}", "title": adv.title, "change_text": adv.change_text,
                          "status": "match", "how": adv.how,
                          "evidence": f"这一局阵亡 {len(deaths)} 次，其中 {k} 次是“{stats.CAUSES[cause]}”（{src}）。",
                          "verify": "下一局继续自动分析；同一个问题累计 5 次确认后进入长期方案，并自动复测效果。"})
        m = self.db.match(v["match_id"]) if v.get("match_id") else None
        return {"items": items, "deaths": len(deaths), "uncaused": sum(1 for e in deaths if not e.get("cause")),
                "coach": (m or {}).get("coach") or "", "label": self._label(v)}

    def m_ai_scan(self, name: str) -> dict:
        ok, why = self.ai_ready()
        if not ok:
            raise UserError(why)
        v = self._view(name)
        if self.game.running and not self.cfg.live_analyze:
            self._pending_add("pending_post", v["session"])
            raise UserError("游戏运行中不做分析，关掉游戏后会自动开始")
        if v["match_id"]:
            m = self.db.match(v["match_id"])
            for e in self.db.events(v["session"]):  # 重新分析：去掉这一局里还没确认的自动标记，手动标的和确认过的保留
                if e["source"] in ("ai", "auto") and not e["confirmed"] and m["t_start"] - 5 <= float(e["t"]) <= m["t_end"] + 5:
                    self.m_delete_event(e["id"])
            self.db.execute("UPDATE matches SET analyzed_at=NULL WHERE id=?", (v["match_id"],))
            # 只用文字识别看过的分段先让 AI 补看（同一条队列，按顺序执行），再重新分析这一局
            self._start_segments(v["session"], final=not self._is_recording_session(v["session"]), notify=False)
            return {"job": self._start_match_job(v["match_id"]).id}
        return {"job": self._start_segments(v["session"], final=not self._is_recording_session(v["session"])).id}

    def _timeline_for_event(self, ev: dict) -> tuple[media.Timeline, float]:
        tl = self._timeline(ev["session"])
        if tl.segments and tl.duration >= float(ev["t"]) + 0.5:
            return tl, 0.0
        if ev.get("clip_path") and Path(ev["clip_path"]).exists():
            return media.Timeline(f"{ev['session']}__clip{ev['id']}", [Path(ev["clip_path"])]), float(ev["clip_start"] or 0)
        raise JobError("原片已清理，截不出这次阵亡的画面")

    def _diagnose(self, ai: analysis.AISession, event_id: int, knocked: bool = False) -> dict:
        ev = self.db.event(event_id)
        if not ev:
            raise JobError("这个标记已经不存在了")
        tl, offset = self._timeline_for_event(ev)
        result = analysis.diagnose(ai, tl, float(ev["t"]), offset)
        if knocked:
            result["knocked"] = True
            result["headline"] = ("被击倒：" + result.get("headline", "")).strip("：")
        fields = {"ai_json": json.dumps(result, ensure_ascii=False), "killer_weapon": result.get("killer_weapon") or "",
                  "distance_m": result.get("distance_m")}
        if not ev.get("confirmed"):
            fields["cause"] = result["cause"]
        self.db.update_event(event_id, **fields)
        return result

    def m_ai_diagnose(self, id: int) -> dict:  # noqa: A002
        ok, why = self.ai_ready()
        if not ok:
            raise UserError(why)
        if self.game.running and not self.cfg.live_analyze:
            self._pending_add("pending_diag", str(int(id)))
            raise UserError("游戏运行中不做分析，关掉游戏后会自动开始")
        return {"job": self._start_diagnose(int(id)).id}

    def _start_diagnose(self, event_id: int):
        ev = self.db.event(event_id)
        if not ev:
            raise UserError("这个标记已经不存在了")

        def work(job):
            ai = self._ai_session(job)
            try:
                job.label = "生成片段"
                try:
                    self._clip_for_event(event_id)
                except media.MediaError as exc:
                    log.info("片段生成失败（继续诊断）：%s", exc)
                job.set_progress(0.4)
                job.label = "AI 诊断阵亡"
                result = self._diagnose(ai, event_id)
            except analysis.Cancelled:
                raise JobCancelled()
            finally:
                ai.close()
            self._pending_remove("pending_diag", str(event_id))
            self._refresh_recs()
            return f"AI 判断：{stats.CAUSES.get(result['cause'], '其他')}（请确认或改成正确的死因）"
        return self.jobs.start("ai_diagnose", "AI 诊断阵亡", work, session=ev["session"], key=str(event_id), pool="ai")

    # =================================================================
    # 建议
    # =================================================================
    def _refresh_recs(self) -> None:
        try:
            events = self._all_events()
            recommend.refresh(self.db, events, self.cfg.profile)
            recommend.evaluate(self.db, events, self._sessions_since)
        except Exception:
            log.exception("更新建议出错")

    def m_adopt_recommendation(self, id: int) -> dict:  # noqa: A002
        recommend.adopt(self.db, int(id), self._all_events())
        return {}

    def _player_profile(self, events: list[dict] | None = None) -> dict:
        events = events if events is not None else self._all_events()
        deaths = recommend.recent_deaths(events, time.time())
        cause, share, k, n = stats.top_cause(deaths)
        dlabel, dcode, dshare, dn = stats.distance_profile([e for e in events if e["kind"] == "death"])
        issue = cause if n >= 3 else ""
        return {"issue": issue, "distance": dcode, "sample_n": n,
                "distance_label": dlabel, "distance_note": f"AI 读出的 {dn} 次交战距离里占 {dshare:.0%}" if dlabel else "",
                "issue_label": stats.CAUSES.get(issue, "") if issue else "",
                "issue_note": f"{n} 次已确认阵亡中 {k} 次（{share:.0%}）" if issue else "",
                "direction": DIRECTION.get(issue, ("", ""))[0], "direction_note": DIRECTION.get(issue, ("", ""))[1]}

    # =================================================================
    # 总览
    # =================================================================
    def _opt_items(self, max_age: float = 20.0) -> list[dict]:
        at, items = self._opt_cache
        if items and time.time() - at < max_age:
            return items
        free = free_gb(self.cfg.recordings_path())
        items = self.opt.items(self.hw, self.cfg.auto_optimize, self._game_exe_path(), free)
        self._opt_cache = (time.time(), items)
        return items

    def _game_exe_path(self) -> str:
        if self._exe_path is None:
            cached = self.db.kv_get("game_exe_path", "") or ""
            self._exe_path = cached if cached and Path(cached).exists() else ""
        return self._exe_path

    def _measurements(self) -> dict:
        def row(label: str | None):
            q = "SELECT * FROM measurements" + (" WHERE label=?" if label else "") + " ORDER BY created DESC LIMIT 1"
            r = self.db.one(q, (label,) if label else ())
            if not r:
                return None
            return {"avg": r["avg"], "low1": r["low1"], "p99": r["p99"], "created": r["created"],
                    "series": json.loads(r["series"] or "[]")}
        return {"before": row("before"), "after": row("after"), "latest": row(None)}

    def m_dashboard(self) -> dict:
        now = time.time()
        items = self._opt_items()
        order = ["refresh", "power", "gamemode", "gamebar", "xmp", "gpupref", "driver", "battery", "storage", "hags",
                 "background"]
        ranked_items = sorted((it for it in items if it.get("check")),
                              key=lambda it: (it["status"] != "warn", order.index(it["key"]) if it["key"] in order else 99))
        m = self._measurements()
        b, a, latest = m["before"], m["after"], m["latest"]
        gain = None
        if b and a and a["created"] > b["created"]:
            gain = {"avg_before": b["avg"], "avg_after": a["avg"], "low_before": b["low1"], "low_after": a["low1"]}
        pre = {"applied": len(self.db.query("SELECT key FROM optimizer_backup")), "auto": self.cfg.auto_optimize,
               "frame_p99": latest["p99"] if latest else None,
               "frametimes": _downsample(latest["series"], 48) if latest else [], "fps_gain": gain,
               "checks": [{"label": it["check"]["label"], "status": it["status"], "value": it["check"]["value"]}
                          for it in ranked_items[:5]], "spec": hwinfo.spec_summary(self.hw)}
        sess = self._sessions()
        events = self._all_events()
        week = {s["name"] for s in sess if (s["started_at"] or 0) >= now - 7 * 86400}
        week_events = [e for e in events if e["view"] in week]
        duel = stats.duel_summary(week_events)
        post = {"sessions_week": len(week), "deaths": sum(1 for e in week_events if stats.counted_death(e)),
                "ai_enabled": self.ai_ready()[0], "unconfirmed": sum(1 for e in events if stats.unconfirmed_ai(e)),
                "duel": {"wins": duel["wins"], "losses": duel["losses"], "rate": duel["rate"]}}
        recent = None
        match_advice = None
        if sess:
            s = sess[0]
            evs = [e for e in events if e["view"] == s["name"]]
            match_advice = self._match_advice(s, evs)
            d = stats.duel_summary(evs)
            meta = self._meta(s)
            recent = {"name": s["name"], "label": self._label(s), "title": self._title(s, meta), "result": meta.get("result") or "",
                      "started_at": s["started_at"], "duration": s["duration"],
                      "deaths": sum(1 for e in evs if e["kind"] == "death"), "duel_total": d["total"], "duel_rate": d["rate"]}
        deaths = recommend.recent_deaths(events, now)
        cause, share, k, n = stats.top_cause(deaths)
        diagnosis = None
        if n >= 3:
            text = f"最近 {n} 次已确认的阵亡里，{k} 次（{share:.0%}）是“{stats.CAUSES[cause]}”。"
            dlabel, _code, dshare, _dn = stats.distance_profile([e for e in deaths if e["cause"] == cause])
            if dlabel:
                text += f"其中 {dshare:.0%} 发生在 {dlabel} 的交战。"
            diagnosis = {"text": text, "n": n}
        recommend.evaluate(self.db, events, self._sessions_since)
        recs, verified = recommend.for_dashboard(self.db, events, self._sessions_since)
        prof = self._player_profile(events)
        codes = gunsmith.ranked(self.db, issue=prof["issue"], distance=prof["distance"], budget=self._budget())
        retention = self._retention_label()
        bitrate = f"{self.cfg.bitrate_mbps} Mbps" if self.cfg.bitrate_mbps else "自动码率"
        return {"pre": pre,
                "mid": {"spec": f"原生分辨率 · {self.cfg.framerate}FPS · {bitrate}",
                        "segment_label": f"{int(self.cfg.segment_seconds)} 秒", "retention_label": retention,
                        "live": self.cfg.live_analyze},
                "post": post, "recent": recent, "match_advice": match_advice, "diagnosis": diagnosis,
                "min_deaths": recommend.MIN_DEATHS,
                "recommendations": recs, "verified": verified,
                "gunsmith_top": next((c for c in codes if c["matched"]), codes[0] if codes else None)}

    # =================================================================
    # 改枪码
    # =================================================================
    def _budget(self) -> str:
        """旧版本存的“30万以上”这类区间已经不用了，当作不限。"""
        return self.cfg.gunsmith_budget if self.cfg.gunsmith_budget in gunsmith.BUDGETS else ""

    def m_gunsmith(self, weapon: str = "", mode: str = "", use: str = "") -> dict:
        events = self._all_events()
        prof = self._player_profile(events)
        budget = self._budget()
        rows = gunsmith.ranked(self.db, issue=prof["issue"], distance=prof["distance"], budget=budget,
                               weapon=weapon, mode=mode, use=use)
        if budget:  # 选了价格区间：标了价格的排前面，没标价格的放后面
            rows.sort(key=lambda c: c["budget_wan"] is None)
        # 按价格区间分组（不受价格筛选影响，只受枪械 / 模式 / 用途筛选影响）
        all_rows = rows if not budget else gunsmith.ranked(self.db, issue=prof["issue"], distance=prof["distance"],
                                                           weapon=weapon, mode=mode, use=use)
        groups = []
        for name in list(gunsmith.BUDGETS) + [gunsmith.UNKNOWN_BUCKET]:
            items = [c for c in all_rows if c["bucket"] == name]
            groups.append({"label": name, "count": len(items), "items": items[:12],
                           "active": budget == name or (not budget)})
        top, rest, seen_v, seen_w = [], [], set(), set()
        best_match = next((c for c in rows if c["matched"]), None)  # 第一个位置留给对症的方案
        if best_match:
            top.append(best_match)
            seen_v.add(best_match["url"])
            seen_w.add(best_match["weapon"])
        for c in rows:  # 前三个尽量来自不同视频、不同枪
            if c is best_match:
                continue
            if len(top) < 3 and c["url"] not in seen_v and c["weapon"] not in seen_w:
                top.append(c)
                seen_v.add(c["url"])
                seen_w.add(c["weapon"])
            else:
                rest.append(c)
        while len(top) < 3 and rest:
            top.append(rest.pop(0))
        everything = gunsmith.ranked(self.db)
        weapon_count: dict[str, int] = {}
        for c in everything:
            weapon_count[c["weapon"]] = weapon_count.get(c["weapon"], 0) + 1
        efficacy = []
        for a in self.db.query("SELECT * FROM adoptions ORDER BY adopted_at DESC"):
            t0 = float(a["adopted_at"])
            before = stats.duel_summary([e for e in events if t0 - 30 * 86400 <= e["at"] < t0])
            after = stats.duel_summary([e for e in events if e["at"] >= t0])
            if after["total"] < 10:
                verdict, ok = f"样本不足（换装后还需 {10 - after['total']} 次对枪）", False
            elif before["rate"] is None:
                verdict, ok = "换装前没有对枪数据，无法对比", False
            else:
                diff = (after["rate"] - before["rate"]) * 100
                verdict = f"胜率{'提升' if diff >= 0 else '下降'} {abs(diff):.0f} 个百分点"
                ok = diff >= 5
                if diff <= -5:
                    verdict += "，可以换回原来的配置"
                elif abs(diff) < 5:
                    verdict = "没有明显变化"
            efficacy.append({"weapon": a["weapon"], "adopted_at": t0, "before_n": before["total"],
                             "before_rate": before["rate"], "after_n": after["total"], "after_rate": after["rate"],
                             "verdict": verdict, "verdict_ok": ok})
        return {"top": top, "rest": rest[:30], "groups": groups,
                "budgets": [{"label": g["label"], "count": g["count"]} for g in groups if g["label"] in gunsmith.BUDGETS],
                "weapons": sorted(weapon_count, key=lambda w: (-weapon_count[w], w)),
                "uses": sorted({u for c in everything for u in c["uses"]}), "budget": budget,
                "crawl": {"last": self.db.kv_get("gunsmith_last"), "count": len(everything),
                          "videos": self.db.kv_get("gunsmith_videos", 0), "running": self.jobs.active("gunsmith")},
                "profile": {k: prof[k] for k in ("sample_n", "distance_label", "distance_note", "issue_label",
                                                 "issue_note", "direction", "direction_note")},
                "efficacy": efficacy}

    def m_gunsmith_refresh(self) -> dict:
        return {"job": self._start_crawl().id}

    def _start_crawl(self, notify: bool = True):
        def work(job):
            seeds = gunsmith.SEED_BVIDS + [b for b in (self.db.kv_get("gunsmith_seeds", []) or [])
                                           if b not in gunsmith.SEED_BVIDS]
            crawler = gunsmith.Crawler(cancelled=lambda: job.cancelled)
            videos, note = gunsmith.crawl(seeds, crawler, progress=job.set_progress)
            if not any(v.codes for v in videos.values()):
                raise JobError(note or "没有抓到改枪码，请检查网络后重试")
            n, nv = gunsmith.store(self.db, videos)
            top = sorted((v for v in videos.values() if v.codes and v.bvid not in gunsmith.SEED_BVIDS),
                         key=lambda v: v.views, reverse=True)[:10]
            self.db.kv_set("gunsmith_seeds", [v.bvid for v in top])
            self.db.kv_set("gunsmith_last", time.time())
            self.db.kv_set("gunsmith_videos", nv)
            return f"社区改枪码已更新：本次读到 {n} 个，来自 {nv} 个视频" + (f"（{note}）" if note else "")
        return self.jobs.start("gunsmith", "抓取社区改枪码", work, pool="net", notify=notify)

    def _maybe_auto_crawl(self) -> None:
        try:
            last = float(self.db.kv_get("gunsmith_last", 0) or 0)
            if time.time() - last > self.cfg.gunsmith_auto_refresh_hours * 3600 and not self.game.running:
                self._start_crawl(notify=last > 0)
        except Exception:
            log.exception("自动抓取改枪码出错")

    def m_code_feedback(self, code: str, ok: bool) -> dict:
        if not self.db.one("SELECT 1 AS x FROM codes WHERE code=?", (code,)):
            raise UserError("没有找到这个改枪码")
        self.db.execute("DELETE FROM code_feedback WHERE code=?", (code,))
        self.db.execute("INSERT INTO code_feedback(code,ok,ts) VALUES(?,?,?)", (code, int(bool(ok)), time.time()))
        return {}

    def m_adopt_code(self, code: str) -> dict:
        row = self.db.one("SELECT weapon FROM codes WHERE code=?", (code,))
        if not row:
            raise UserError("没有找到这个改枪码")
        if self.db.one("SELECT 1 AS x FROM adoptions WHERE code=?", (code,)):
            self.db.execute("DELETE FROM adoptions WHERE code=?", (code,))
            return {"adopted": False}
        self.db.execute("DELETE FROM adoptions WHERE weapon=?", (row["weapon"],))
        self.db.execute("INSERT INTO adoptions(code,weapon,adopted_at) VALUES(?,?,?)", (code, row["weapon"], time.time()))
        return {"adopted": True}

    # =================================================================
    # 赛前优化
    # =================================================================
    def _job_collect_hw(self, job) -> str:
        self.hw = hwinfo.collect()
        job.set_progress(0.4)
        self._opt_cache = (0.0, [])
        try:
            self._select_encoder()
        except JobError as exc:
            log.warning("%s", exc)
        job.set_progress(0.6)
        if IS_WINDOWS:
            if self.game_exe and not self._game_exe_path():
                found = optimizer.find_game_exe([self.game_exe])
                if found:
                    self.db.kv_set("game_exe_path", found)
                    self._exe_path = found
            self.opt.sample_background()
        job.set_progress(0.9)
        if self.cfg.auto_optimize and not self.game.running and IS_WINDOWS:
            self._auto_persistent()
        self._opt_cache = (0.0, [])
        return "硬件状态已重新扫描"

    def _auto_persistent(self) -> list[str]:
        hybrid = optimizer.is_hybrid([n.lower() for n in hwinfo.gpu_names(self.hw)])
        msgs = self.opt.auto_persistent(self._game_exe_path(), hybrid)
        self._opt_cache = (0.0, [])
        if msgs:
            log.info("赛前自动优化：%s", "；".join(msgs))
        return msgs

    def _select_encoder(self, force: bool = False) -> str:
        if self.cfg.encoder != "auto":
            self.controller.encoder = self.cfg.encoder
            return f"使用手动选择的编码器 {self.cfg.encoder}"
        gpus = hwinfo.gpu_names(self.hw)
        cache = self.cfg.encoder_cache or {}
        if not force and cache.get("encoder") and cache.get("gpu") == ",".join(gpus) and \
                cache.get("v") == encoders.ENCODER_SPEC_VERSION:
            self.controller.encoder = cache["encoder"]
            return f"录制编码器：{cache['encoder']}"
        if not IS_WINDOWS:
            return "只有 Windows 上才能录制"
        if not self.controller.ffmpeg:
            raise JobError("缺少 FFmpeg，请重新运行 setup.bat")
        if self.controller.recording:
            raise JobError("正在录制，请关掉游戏后再检测编码器")
        self.encoder_busy = True
        try:
            enc, report = encoders.select_encoder(self.controller.ffmpeg, gpus, 0)
        finally:
            self.encoder_busy = False
        self.encoder_report = report
        self.controller.encoder = enc
        if not enc:
            self.encoder_failed = "显卡编码器自检失败，暂时无法录制（设置 → 重新检测编码器；仍失败请导出诊断包）"
            raise JobError("显卡硬件编码器自检全部失败，暂时无法录制。请在设置里导出诊断包发给开发者")
        self.encoder_failed = ""
        with self._cfg_lock:
            self.cfg.encoder_cache = {"encoder": enc, "gpu": ",".join(gpus), "v": encoders.ENCODER_SPEC_VERSION}
            self._save_cfg()
        return f"录制编码器：{enc}"

    def m_optimizer(self) -> dict:
        items = self._opt_items(max_age=5)
        shown = [it for it in items if it["status"] != "unknown"]
        m = self._measurements()
        return {"pass": sum(1 for it in shown if it["status"] in ("ok", "applied")), "total": len(shown),
                "scanned_at": self.hw.get("collected_at"), "auto": self.cfg.auto_optimize,
                "presentmon": bool(perfmon.find_presentmon()), "measuring": self.jobs.active("measure"),
                "next_label": self.cfg.next_measure_label, "profile_text": self._profile_text(), "mdv": "",
                "measure": {"before": m["before"], "after": m["after"]}, "items": items,
                "specs": hwinfo.spec_groups(self.hw), "spec_summary": hwinfo.spec_summary(self.hw)}

    def _profile_text(self) -> str:
        p = self.cfg.profile or {}

        def f(key, digits=2):
            v = p.get(key)
            return f"{v:.{digits}f}" if isinstance(v, (int, float)) else "—"
        if not any(isinstance(p.get(k), (int, float)) for k in PROFILE_LIMITS):
            return ""
        return (f"鼠标 {f('mouse')} · 垂直/水平 {f('vertical')}/{f('horizontal')} · "
                f"举枪 {f('ads_boost')}/{f('ads_vertical')}/{f('ads_horizontal')}")

    def m_optimize_apply(self, key: str) -> dict:
        if key == "refresh" and self.game.running:
            return {"ok": False, "message": "游戏运行中改刷新率可能让游戏画面闪黑，请关掉游戏后再改"}
        ok, msg = self.opt.apply(key, self._game_exe_path())
        self._opt_cache = (0.0, [])
        return {"ok": ok, "message": msg}

    def m_optimize_restore(self, key: str | None = None) -> dict:
        msg = self.opt.restore(key)
        self._opt_cache = (0.0, [])
        return {"message": msg}

    def m_rescan_hw(self) -> dict:
        return {"job": self.jobs.start("hw", "重新扫描硬件", self._job_collect_hw, pool="sys").id}

    def m_reprobe_encoder(self) -> dict:
        def work(job):
            return self._select_encoder(force=True)
        return {"job": self.jobs.start("encoder", "检测显卡编码器", work, pool="sys").id}

    def m_measure_fps(self, seconds: int = 60) -> dict:
        if not self.game.running:
            raise UserError("先打开游戏进入对局，再开始测量")
        exe = self.game.window.exe if self.game.window else self.game_exe
        if not exe:
            raise UserError("还没识别出游戏进程名，请在游戏窗口在前台时再试")
        seconds = max(20, min(180, int(seconds)))
        label = self.cfg.next_measure_label if self.cfg.next_measure_label in ("before", "after") else "before"

        def work(job):
            start = time.time()
            done = threading.Event()

            def ticker():
                while not done.wait(1):
                    job.set_progress(min(0.95, (time.time() - start) / (seconds + perfmon.START_DELAY + 5)))
            threading.Thread(target=ticker, daemon=True).start()
            try:
                result = perfmon.measure(exe, seconds)
            finally:
                done.set()
            self.db.execute("INSERT INTO measurements(created,label,process,seconds,frames,avg,low1,p99,series) "
                            "VALUES(?,?,?,?,?,?,?,?,?)",
                            (time.time(), label, exe, seconds, result["frames"], result["avg"], result["low1"],
                             result["p99"], json.dumps(result["series"])))
            if label == "before":
                with self._cfg_lock:
                    self.cfg.next_measure_label = "after"
                    self._save_cfg()
            return f"测量完成（{'优化前' if label == 'before' else '优化后'}）：平均 {result['avg']:.0f} FPS，1% Low {result['low1']:.0f} FPS"
        return {"job": self.jobs.start("measure", "测量帧率", work, pool="sys").id}

    # =================================================================
    # 设置
    # =================================================================
    def m_settings(self) -> dict:
        c = self.cfg
        key = self._api_key()
        backups = self.opt.changed_items()
        storage = self._storage_info()
        providers = [{"id": pid, "label": spec["label"], "base_url": spec["base_url"], "vision": spec["vision"],
                      "text": spec["text"], "console": spec["console"], "note": spec["note"],
                      "has_key": bool(secrets_store.get_api_key(pid))} for pid, spec in PROVIDERS.items()]
        return {"build": BUILD, "fingerprint": self._source_fingerprint(), "app_root": str(paths.app_root()),
                "encoder": self.controller.encoder, "encoders": encoders.known_encoders(),
                "storage": {"path": str(c.recordings_path()), "free_bytes": storage["free_bytes"]},
                "config": {**{k: getattr(c, k) for k in (
                    "retention_days", "framerate", "bitrate_mbps", "encoder", "monitor_index", "ai_provider",
                    "ai_text_model", "ai_vision_model", "ai_daily_token_limit", "ai_vision_enabled",
                    "game_title_keywords", "game_process_names", "profile", "live_analyze")},
                    "ai_base_url": self._base_url()},
                "ai": {"configured": bool(key), "key_mask": secrets_store.mask(key), "providers": providers,
                       "provider": self._provider()["id"],
                       "models": self.db.kv_get("ai_models", []) or [], "used_today": self.db.usage_today(),
                       "last_output": self.db.kv_get("ai_last_output", "") or "",
                       "vision_status": self._vision_status()},
                "db": self.db.stats(), "restore": {"count": len(backups), "items": backups}}

    def _vision_status(self) -> str:
        if not self._api_key():
            return ""
        model = self.cfg.ai_vision_model
        if model and self.db.kv_get("vision_ok", "") == self._vision_key():
            return f"看图自检通过：{model} 能看到图片"
        fail = self.db.kv_get("vision_fail", {}) or {}
        if fail.get("key") == self._vision_key() and fail.get("message"):
            return fail["message"]
        return "还没做看图自检（开启 AI 看图后会自动检查）"

    def _source_fingerprint(self) -> str:
        if not self._fingerprint:
            h = hashlib.sha256()
            root = paths.app_root() / "fupan"
            for p in sorted(root.rglob("*")):
                if p.is_file() and p.suffix in (".py", ".js", ".css", ".html") and "__pycache__" not in p.parts:
                    h.update(str(p.relative_to(root)).encode("utf-8"))
                    h.update(p.read_bytes())
            digest = h.hexdigest()
            self._fingerprint = digest[:8] + "…" + digest[-4:]
        return self._fingerprint

    def m_save_settings(self, **values) -> dict:
        if len(values) != 1:
            raise UserError("一次只能改一项设置")
        (key, value), = values.items()
        c = self.cfg
        enum = {"retention_days": {0, 1, 3, 7}, "framerate": {30, 60}, "bitrate_mbps": {0, 12, 20, 30, 45},
                "monitor_index": {-1, 0, 1, 2, 3}, "ai_daily_token_limit": {200000, 500000, 1000000, 3000000},
                "gunsmith_budget": BUDGET_OPTIONS, "next_measure_label": {"before", "after"},
                "encoder": {"auto", *encoders.known_encoders()}, "ai_provider": set(PROVIDERS)}
        if key in enum:
            if value not in enum[key] or isinstance(value, bool):
                raise UserError("这个设置的取值不对")
        elif key in ("ai_vision_enabled", "auto_optimize", "post_auto_analyze", "restore_power_after_game", "live_analyze"):
            if not isinstance(value, bool):
                raise UserError("这个设置的取值不对")
        elif key in ("game_title_keywords", "game_process_names"):
            if not isinstance(value, list):
                raise UserError("这个设置的取值不对")
            value = [str(v).strip()[:60] for v in value if str(v).strip()][:10]
            if key == "game_title_keywords" and not value:
                raise UserError("至少要留一个标题关键词")
        elif key == "ai_base_url":
            value = str(value).strip().rstrip("/")
            if value:
                u = urlparse(value)
                if u.scheme != "https" or not u.netloc:
                    raise UserError("接口地址必须是 https:// 开头（密钥会发到这个地址）")
            if value == self._provider()["base_url"]:
                value = ""
        elif key in ("ai_text_model", "ai_vision_model"):
            value = str(value).strip()[:80]
        else:
            raise UserError("不能修改这个设置")
        with self._cfg_lock:
            setattr(c, key, value)
            if key == "ai_provider":  # 换平台：地址和模型都换成这个平台推荐的
                spec = PROVIDERS[value]
                c.ai_base_url = ""
                c.ai_text_model, c.ai_vision_model = spec["text"][0], spec["vision"][0]
            self._save_cfg()
        if key == "encoder":
            if value == "auto":
                self.m_reprobe_encoder()
            else:
                self.controller.encoder = value
        elif key == "retention_days":
            self._cleanup_old()
        elif key == "auto_optimize":
            self._opt_cache = (0.0, [])
            if value and not self.game.running:
                self._start_auto_opt()
        if key in ("ai_provider", "ai_base_url"):
            self.db.kv_set("ai_models", [])
            self._key = None
            self._ai_pause_until = 0.0
        if key in ("ai_provider", "ai_base_url", "ai_vision_model", "ai_vision_enabled"):
            self.db.kv_set("vision_fail", {})
            self._sessions_cache = (0.0, [])
            if self.cfg.ai_vision_enabled and self._api_key():
                self.ai_ready()  # 需要的话马上开始看图自检
        return {}

    def _start_auto_opt(self):
        def work(job):
            if self.game.running:
                return "游戏运行中，等游戏关闭后再处理刷新率等设置"
            msgs = self._auto_persistent()
            return "赛前优化已完成：" + "；".join(msgs) if msgs else "检查完了，系统设置已经是适合打游戏的状态"
        return self.jobs.start("opt_auto", "赛前自动优化", work, pool="sys")

    def m_save_profile(self, **values) -> dict:
        profile = dict(self.cfg.profile or {})
        for key, value in values.items():
            if key not in PROFILE_LIMITS:
                raise UserError("不能修改这个设置")
            if value is None:
                profile.pop(key, None)
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise UserError("请填数字")
            lo, hi = PROFILE_LIMITS[key]
            if not lo <= float(value) <= hi:
                raise UserError(f"这个数值不对（范围 {lo}～{hi}）")
            profile[key] = float(value)
        with self._cfg_lock:
            self.cfg.profile = profile
            self._save_cfg()
        self._refresh_recs()
        return {}

    def m_set_api_key(self, key: str) -> dict:
        key = (key or "").strip()
        if len(key) < 16 or any(ch.isspace() for ch in key):
            raise UserError("密钥格式不对，请完整复制控制台里的 API Key")
        provider = self._provider()["id"]
        secrets_store.set_api_key(provider, key)
        self._key, self._key_provider = key, provider
        self.db.kv_set("vision_ok", "")
        self.db.kv_set("vision_fail", {})
        self._ai_pause_until = 0.0

        def work(job):
            self._list_models()
            if self.cfg.ai_vision_enabled:
                self.ai_ready()
            return (f"已读取 {self._provider()['label']} 的模型：文字模型 {self.cfg.ai_text_model or '无'}，"
                    f"看图模型 {self.cfg.ai_vision_model or '没有找到'}")
        self.jobs.start("models", "读取模型列表", work, pool="net")
        return {}

    def m_delete_api_key(self) -> dict:
        secrets_store.delete_api_key(self._provider()["id"])
        self._key = ""
        return {}

    def _list_models(self) -> list[str]:
        if not self._api_key():
            raise UserError("请先填写并保存 API Key")
        with self._client(timeout=30) as client:
            models = client.list_models()
        self.db.kv_set("ai_models", models)
        provider = self._provider()
        text, vision = pick_default_models(models, provider["id"])
        with self._cfg_lock:
            if not self.cfg.ai_text_model or (models and self.cfg.ai_text_model not in models):
                self.cfg.ai_text_model = text
            if not self.cfg.ai_vision_model or (models and self.cfg.ai_vision_model not in models) or \
                    self.cfg.ai_vision_model in provider["text_only"]:
                self.cfg.ai_vision_model = vision
            self._save_cfg()
        return models

    def m_ai_test(self, kind: str, path: str = "") -> dict:
        if not self._api_key():
            raise UserError("请先填写并保存 API Key")
        limit = self.cfg.ai_daily_token_limit
        if kind != "models" and limit and self.db.usage_today() >= limit:
            raise UserError("今天的 AI 用量已到上限，可以在下面调高")
        label = self._provider()["label"]
        if kind == "models":
            models = self._list_models()
            listed = (f"账号可用 {len(models)} 个模型：\n" + "\n".join(models)) if models else \
                "这个平台没有模型列表接口，使用推荐模型（也可以在下面手动填写模型名）"
            text = (f"{label} 连接正常。{listed}"
                    f"\n\n已选文字模型：{self.cfg.ai_text_model or '无'}"
                    f"\n已选看图模型：{self.cfg.ai_vision_model or '没有找到能看图的模型'}"
                    f"\n{self._vision_status()}")
        elif kind == "text":
            with self._client(timeout=60) as client:
                r = client.chat(self.cfg.ai_text_model, [{"role": "user", "content":
                                "用一句话回答：三角洲行动的烽火地带模式里，“撤离成功”是什么意思？"}], max_tokens=300)
            self.db.add_usage(r.model, "test", r.prompt_tokens, r.completion_tokens)
            text = f"[{r.model}] {r.text.strip() or '（没有返回正文）'}\n（本次用量 {r.prompt_tokens + r.completion_tokens} tokens）"
        elif kind in ("image-file", "image-recording"):
            if not self.cfg.ai_vision_model:
                raise UserError("还没有看图模型：先点“测试连接”")
            img = self._test_image(kind, path)
            with self._client(timeout=90) as client:
                r = client.ask_with_image(self.cfg.ai_vision_model, HUD_TEST_PROMPT, img)
            self.db.add_usage(r.model, "test", r.prompt_tokens, r.completion_tokens)
            text = f"[{r.model}]\n{r.text.strip() or '（没有返回正文）'}\n（本次用量 {r.prompt_tokens + r.completion_tokens} tokens）"
        elif kind == "vision":
            self.db.kv_set("vision_fail", {})
            self.db.kv_set("vision_ok", "")
            job = self._start_vision_check()
            deadline = time.time() + 90
            while job.status in ("queued", "running") and time.time() < deadline:
                time.sleep(0.3)
            text = job.message or self._vision_status()
        else:
            raise UserError("测试类型不对")
        self.db.kv_set("ai_last_output", text)
        return {"text": text}

    def _test_image(self, kind: str, path: str) -> bytes:
        if kind == "image-file":
            p = Path(path or "")
            if not p.is_file():
                raise UserError("没有选择图片")
            if p.stat().st_size > 20 * 1024 ** 2:
                raise UserError("图片太大（超过 20 MB）")
            ffmpeg = encoders.find_ffmpeg()
            if ffmpeg:
                out = media.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(p), "-frames:v", "1",
                                 "-vf", "scale='min(1600,iw)':-2", "-q:v", "3", "-f", "image2pipe", "-vcodec", "mjpeg", "-"], 60)
                if out.returncode == 0 and out.stdout:
                    return out.stdout
            return p.read_bytes()
        for s in self._sessions():
            if s["files"] and not s["active"]:
                tl = media.Timeline(s["session"], s["files"])
                if tl.segments:
                    t = (s["t0"] + (s["t1"] or tl.duration)) / 2
                    img = tl.frame(min(t, tl.duration * 0.99), width=1600, mask=True)
                    if img:
                        return img
        raise UserError("还没有可用的录像：先录一局游戏")

    def m_pick_folder(self, path: str = "") -> dict:
        if not path:
            return {}
        p = Path(path)
        if not p.is_dir():
            raise UserError("文件夹不存在")
        with self._cfg_lock:
            self.cfg.recordings_dir = str(p)
            self._save_cfg()
        self._storage = (0.0, self._storage[1])
        self._sessions_cache = (0.0, [])
        return {"path": str(p)}

    def m_list_windows(self) -> list[dict]:
        return [{"exe": w.exe, "title": w.title, "width": w.rect.width, "height": w.rect.height}
                for w in describe_windows_for_picker()]

    def m_add_game_process(self, exe: str) -> dict:
        exe = str(exe or "").strip()
        if not exe.lower().endswith(".exe"):
            raise UserError("请选择游戏本体的窗口")
        with self._cfg_lock:
            names = [n for n in self.cfg.game_process_names if n.lower() != exe.lower()]
            self.cfg.game_process_names = names + [exe]
            self._save_cfg()
        return {}

    def m_set_auto_record(self, on: bool) -> dict:
        with self._cfg_lock:
            self.cfg.auto_record = bool(on)
            self._save_cfg()
        return {}

    def m_finish_first_run(self, auto_optimize: bool = False, ai_vision_enabled: bool = False) -> dict:
        with self._cfg_lock:
            self.cfg.auto_optimize = bool(auto_optimize)
            self.cfg.ai_vision_enabled = bool(ai_vision_enabled)
            self.cfg.first_run_done = True
            self._save_cfg()
        if auto_optimize and not self.game.running and IS_WINDOWS:
            self._start_auto_opt()
        return {}

    # ---- 数据 ----
    def m_export_data(self) -> dict:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        target = paths.desktop_dir() / f"三角洲复盘数据_{stamp}.zip"
        tmp = paths.data_dir() / f"export_{stamp}.db"
        try:
            self.db.backup_to(tmp)
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(tmp, "fupan.db")
                for table, cols in (("events", "session,t,kind,cause,source,confirmed,killer_weapon,distance_m"),
                                    ("session_meta", "name,mode,map,result,imported,started_at,duration"),
                                    ("measurements", "created,label,process,seconds,frames,avg,low1,p99"),
                                    ("recommendations", "rule,title,status,created,adopted_at,result"),
                                    ("codes", "code,weapon,mode,author,url,views,favorites,invalid")):
                    buf = io.StringIO()
                    writer = csv.writer(buf)
                    names = cols.split(",")
                    writer.writerow(names)
                    for row in self.db.query(f"SELECT {cols} FROM {table}"):
                        writer.writerow([row[n] for n in names])
                    zf.writestr(f"{table}.csv", "﻿" + buf.getvalue())
                zf.writestr("说明.txt", "本地数据导出：标注、场次信息、帧率测量、建议记录、改枪码缓存。不含录像和 API Key。\n"
                                        "csv 文件可以用 Excel 打开；fupan.db 是完整的 SQLite 数据库。")
        finally:
            tmp.unlink(missing_ok=True)
        return {"file": target.name}

    def m_reset_data(self) -> dict:
        self.db.reset()
        shutil.rmtree(paths.data_dir() / "clips", ignore_errors=True)
        self._sessions_cache = (0.0, [])
        return {}

    def m_export_diagnostics(self) -> dict:
        exes = [w.exe for w in winapi.list_windows() if w.visible and w.rect.width >= 1024]
        recent_sessions = [r["session"] for r in self.db.query(
            "SELECT DISTINCT session FROM frame_states ORDER BY t DESC LIMIT 200")][:3]
        states = {}
        for name in dict.fromkeys(recent_sessions):
            counts: dict[str, int] = {}
            src: dict[str, int] = {}
            kw: dict[str, int] = {}
            for f in self.db.states(name):
                counts[f["state"]] = counts.get(f["state"], 0) + 1
                src[f.get("src") or "?"] = src.get(f.get("src") or "?", 0) + 1
                if f.get("kw"):
                    kw[f["kw"]] = kw.get(f["kw"], 0) + 1
            states[name] = {"states": counts, "src": src, "ocr_keywords": kw}
        extra = {"jobs": self.jobs.list(), "db": self.db.stats(), "optimizer_changed": self.opt.changed_items(),
                 "recorder": {"output_changes": self.controller.output_changes, "failures": self.controller.failures,
                              "status": self.controller.status, "encoder": self.controller.encoder},
                 "vision": self._vision_status(), "ai_error": self._ai_error, "states": states, "ocr": ocr.status(),
                 "matches": [{k: m[k] for k in ("session", "t_start", "t_end", "closed", "map", "mode", "result",
                                                "deaths_json", "analyzed_at")} for m in self.db.matches()[-10:]],
                 "ai_ready": self.ai_ready(), "game_exe": self.game_exe, "presentmon": perfmon.find_presentmon(),
                 "ffmpeg": encoders.find_ffmpeg(), "vp9": media.vp9_encoder() if encoders.find_ffmpeg() else ""}
        path = diagnostics.export(self.cfg, self.hw, self.encoder_report, exes, extra)
        return {"file": path.name}

    # ---- 外壳能力 ----
    def m_open_path(self, path: str = "", kind: str = "") -> dict:
        p = paths.log_dir() if kind == "logs" else Path(path or "")
        if not str(p) or not p.exists():
            raise UserError("文件夹不存在（可能已被清理）")
        if p.is_file():  # 只打开文件夹，不运行任何文件
            p = p.parent
        self.host.open_path(str(p))
        return {}

    def m_open_url(self, url: str) -> dict:
        u = urlparse(str(url or ""))
        if u.scheme != "https" or u.hostname not in OPEN_URL_HOSTS:
            raise UserError("这个链接不能从软件里打开")
        self.host.open_url(u.geturl())
        return {}

    def m_copy_text(self, text: str) -> dict:
        self.host.copy_text(str(text or "")[:4000])
        return {}
