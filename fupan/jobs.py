"""后台任务：生成片段、缩略图、AI 分析、抓改枪码、测帧率……界面每秒读一次列表显示进度，做完弹提示。

几条队列互不阻塞：ai（一次一个，避免并发扣费）、media（转码）、net（联网）、sys（改系统设置、测帧率）。
同一件事（同类型 + 同场次 + 同对象）排着队时不会重复添加。
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from .aiclient import AIError

log = logging.getLogger(__name__)

POOLS = {"ai": 1, "media": 2, "net": 2, "sys": 1}
KEEP_FINISHED_SEC = 300


class JobError(Exception):
    """给用户看的失败原因（中文）。"""


class JobCancelled(Exception):
    """被取消了（比如游戏启动了）。"""


class Job:
    def __init__(self, job_id: int, kind: str, label: str, session: str | None, key: str, notify: bool):
        self.id = job_id
        self.kind = kind
        self.label = label
        self.session = session
        self.key = key
        self.notify = notify
        self.status = "queued"
        self.progress = 0.0
        self.message = ""
        self.created = time.time()
        self.finished = 0.0
        self.cancel_event = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def check(self) -> None:
        if self.cancel_event.is_set():
            raise JobCancelled()

    def set_progress(self, value: float) -> None:
        self.progress = max(0.0, min(1.0, float(value)))

    def to_dict(self) -> dict:
        active = self.status in ("queued", "running")
        return {"id": self.id, "kind": self.kind, "session": self.session,
                "label": self.label + ("（排队中）" if self.status == "queued" else ""),
                "progress": round(self.progress, 3), "status": "running" if active else self.status,
                "message": self.message, "notify": self.notify}


class Jobs:
    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: dict[int, Job] = {}
        self._ids = itertools.count(1)
        self._pools = {name: ThreadPoolExecutor(max_workers=n, thread_name_prefix=f"job-{name}")
                       for name, n in POOLS.items()}

    def start(self, kind: str, label: str, fn, *, session: str | None = None, key: str = "", pool: str = "media",
              notify: bool = True) -> Job:
        """fn(job) 返回完成提示；抛 JobError 表示失败并显示原因。"""
        dedup = f"{kind}|{session or ''}|{key}"
        with self._lock:
            for job in self._jobs.values():
                if job.key == dedup and job.status in ("queued", "running"):
                    return job
            job = Job(next(self._ids), kind, label, session, dedup, notify)
            self._jobs[job.id] = job
        self._pools[pool].submit(self._run, job, fn)
        return job

    def _run(self, job: Job, fn) -> None:
        if job.cancelled:
            self._finish(job, "cancelled", "已取消", notify=False)
            return
        job.status = "running"
        try:
            message = fn(job) or "完成"
            self._finish(job, "done", message)
        except JobCancelled:
            self._finish(job, "cancelled", "游戏已启动，分析先暂停，关掉游戏后自动继续", notify=False)
        except (JobError, AIError) as exc:
            log.info("任务失败 %s：%s", job.kind, exc)
            self._finish(job, "error", str(exc))
        except Exception as exc:  # 程序错误：记录完整报错，界面只显示一句话
            log.error("任务出错 %s：%s\n%s", job.kind, exc, traceback.format_exc())
            self._finish(job, "error", f"{job.label}出错了：{exc}")

    def _finish(self, job: Job, status: str, message: str, notify: bool | None = None) -> None:
        job.status = status
        job.message = message
        job.finished = time.time()
        job.progress = 1.0 if status == "done" else job.progress
        if notify is not None:
            job.notify = notify

    def list(self) -> list[dict]:
        now = time.time()
        with self._lock:
            for job_id in [j.id for j in self._jobs.values()
                           if j.finished and now - j.finished > KEEP_FINISHED_SEC]:
                del self._jobs[job_id]
            jobs = list(self._jobs.values())
        # 界面不显示被取消的任务
        return [j.to_dict() for j in jobs if j.status != "cancelled"]

    def active(self, kind: str | None = None, session: str | None = None) -> bool:
        with self._lock:
            return any(j.status in ("queued", "running") and (kind is None or j.kind == kind)
                       and (session is None or j.session == session) for j in self._jobs.values())

    def cancel(self, kinds: set[str]) -> list[Job]:
        with self._lock:
            hit = [j for j in self._jobs.values() if j.kind in kinds and j.status in ("queued", "running")]
        for j in hit:
            j.cancel_event.set()
        return hit

    def shutdown(self) -> None:
        with self._lock:
            for j in self._jobs.values():
                j.cancel_event.set()
        for pool in self._pools.values():
            pool.shutdown(wait=False, cancel_futures=True)
