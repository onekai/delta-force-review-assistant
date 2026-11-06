"""录像处理：把一个场次的多个分段文件拼成一条时间轴，生成缩略图、回放片段、截帧和拼图。

时间轴用“录像时间”：所有分段按顺序首尾相接（切到后台暂停的那段时间不计入）。
回放片段转成 VP9 WebM，因为软件界面用的网页组件不能播放 H.264。
片段和缩略图放在数据目录下，原片按保留天数清理后它们仍然保留。
所有处理都用最低的进程优先级（IDLE），游戏开着时也不和游戏抢 CPU。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from shutil import which

from . import paths
from .encoders import CREATE_NO_WINDOW, find_ffmpeg

log = logging.getLogger(__name__)

VIDEO_EXTS = {".mkv", ".mp4", ".mov", ".avi", ".flv", ".webm", ".m4v", ".ts"}
_SEGMENT_RE = re.compile(r"run(\d+)_(\d+)\.mkv$", re.I)
INDEX_FILE = "media_index.json"
IDLE_PRIORITY_CLASS = 0x00000040 if sys.platform == "win32" else 0
# 游戏左上角有 UID 水印，发给 AI 的画面把这一块涂黑（只影响发给 AI 的截图，不影响录像）
AI_MASK = "drawbox=x=0:y=0:w=iw*0.42:h=ih*0.06:color=black:t=fill"


class MediaError(RuntimeError):
    """给用户看的错误，已经是中文。"""


def find_ffprobe() -> str:
    ffmpeg = find_ffmpeg()
    name = "ffprobe.exe" if sys.platform == "win32" else "ffprobe"
    if ffmpeg:
        local = Path(ffmpeg).with_name(name)
        if local.exists():
            return str(local)
    return which(name) or ""


def run(cmd: list[str], timeout: float = 600) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout,
                              creationflags=CREATE_NO_WINDOW | IDLE_PRIORITY_CLASS)
    except subprocess.TimeoutExpired as exc:
        raise MediaError("处理录像超时") from exc
    except OSError as exc:
        raise MediaError(f"无法运行 FFmpeg：{exc}") from exc


def _need_ffmpeg() -> str:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise MediaError("缺少 FFmpeg，请重新运行 setup.bat")
    return ffmpeg


def _stderr(result: subprocess.CompletedProcess) -> str:
    return result.stderr.decode("utf-8", "replace").strip()[-300:]


def probe(path: Path) -> dict:
    """时长、分辨率、帧率。读不出来的项为 0。"""
    info = {"duration": 0.0, "width": 0, "height": 0, "fps": 0.0}
    ffprobe = find_ffprobe()
    if ffprobe:
        try:
            out = run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                       "stream=width,height,avg_frame_rate:format=duration", "-of", "json", str(path)], 60)
            data = json.loads(out.stdout.decode("utf-8", "replace") or "{}")
        except (MediaError, ValueError):  # ffprobe 坏了就用 ffmpeg 读
            data = {}
        stream = (data.get("streams") or [{}])[0]
        info["width"] = int(stream.get("width") or 0)
        info["height"] = int(stream.get("height") or 0)
        rate = str(stream.get("avg_frame_rate") or "0/1")
        num, _, den = rate.partition("/")
        try:
            info["fps"] = round(float(num) / float(den or 1), 2) if float(den or 1) else 0.0
        except ValueError:
            pass
        try:
            info["duration"] = max(0.0, float((data.get("format") or {}).get("duration") or 0))
        except ValueError:
            pass
        if info["duration"]:
            return info
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return info
    text = run([ffmpeg, "-hide_banner", "-i", str(path)], 60).stderr.decode("utf-8", "replace")
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if m:
        info["duration"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", text)
    if m and not info["width"]:
        info["width"], info["height"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"Video:.*?(\d+(?:\.\d+)?) fps", text)
    if m and not info["fps"]:
        info["fps"] = float(m.group(1))
    return info


@dataclass
class Segment:
    path: str
    start: float
    duration: float
    size: int
    mtime: float
    width: int = 0
    height: int = 0
    fps: float = 0.0


def segment_sort_key(p: Path):
    m = _SEGMENT_RE.search(p.name)
    return (int(m.group(1)), int(m.group(2))) if m else (9999, 0)


def list_video_files(folder: Path) -> list[Path]:
    """录制的场次文件夹里只取 runXX_YYY.mkv；别的文件夹取所有视频文件。"""
    if not folder.exists() or not folder.is_dir():
        return []
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
    recorded = [p for p in files if _SEGMENT_RE.search(p.name)]
    return sorted(recorded or files, key=lambda p: (segment_sort_key(p), p.name))


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _session_lock(kind: str, session: str) -> threading.Lock:
    """同一场录像的缩略图 / 拼图同时只让一个线程生成，避免互相覆盖。"""
    with _locks_guard:
        return _locks.setdefault(f"{kind}|{session}", threading.Lock())


def session_cache_dir(kind: str, session: str) -> Path:
    """kind: media / clips / thumbs。"""
    d = paths.data_dir() / kind / session
    d.mkdir(parents=True, exist_ok=True)
    return d


class Timeline:
    """一个场次的时间轴。分段信息缓存在数据目录，文件大小或修改时间变了就重新读取。"""

    def __init__(self, session: str, files: list[Path]):
        self.session = session
        self.files = files
        self.segments: list[Segment] = []
        self._load()

    def _load(self) -> None:
        index_path = session_cache_dir("media", self.session) / INDEX_FILE
        cached: dict[str, dict] = {}
        try:
            for item in json.loads(index_path.read_text(encoding="utf-8")):
                cached[item["path"]] = item
        except (OSError, ValueError, KeyError, TypeError):
            pass
        start = 0.0
        segments = []
        changed = False
        for f in self.files:
            try:
                st = f.stat()
            except OSError:
                continue
            item = cached.get(str(f))
            if item and item.get("size") == st.st_size and abs(item.get("mtime", 0) - st.st_mtime) < 1 \
                    and item.get("duration"):
                info = {k: item.get(k, 0) for k in ("duration", "width", "height", "fps")}
            else:
                info = probe(f)
                changed = True
            if info["duration"] <= 0:
                continue
            segments.append(Segment(str(f), start, float(info["duration"]), st.st_size, st.st_mtime,
                                    int(info["width"]), int(info["height"]), float(info["fps"])))
            start += float(info["duration"])
        self.segments = segments
        if changed or len(cached) != len(segments):
            try:
                index_path.write_text(json.dumps([asdict(s) for s in segments], ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass

    @property
    def duration(self) -> float:
        return sum(s.duration for s in self.segments)

    @property
    def resolution(self) -> tuple[int, int]:
        for s in self.segments:
            if s.width:
                return s.width, s.height
        return 0, 0

    def locate(self, t: float) -> tuple[Segment, float] | None:
        if not self.segments:
            return None
        t = max(0.0, min(t, self.duration - 0.05))
        for seg in self.segments:
            if seg.start <= t < seg.start + seg.duration:
                return seg, t - seg.start
        last = self.segments[-1]
        return last, max(0.0, last.duration - 0.05)

    def pieces(self, t0: float, t1: float) -> list[tuple[Segment, float, float]]:
        """[t0, t1) 跨越的各分段：(分段, 段内起点, 时长)。"""
        out = []
        t0, t1 = max(0.0, t0), min(self.duration, t1)
        for seg in self.segments:
            a, b = max(t0, seg.start), min(t1, seg.start + seg.duration)
            if b - a > 0.05:
                out.append((seg, a - seg.start, b - a))
        return out

    # ---- 回放片段 ----
    def make_clip(self, t_center: float, before: float = 12.0, after: float = 3.0, height: int = 720) -> dict:
        if not self.segments:
            raise MediaError("原片已清理或不存在，无法生成片段")
        t0, t1 = max(0.0, t_center - before), min(self.duration, t_center + after)
        parts = self.pieces(t0, t1)
        if not parts:
            raise MediaError("这个时间点没有录像")
        ffmpeg = _need_ffmpeg()
        out = session_cache_dir("clips", self.session) / f"clip_{t0:08.2f}_{t1:08.2f}.webm"
        length = sum(p[2] for p in parts)
        if out.exists() and out.stat().st_size > 0:
            return {"path": str(out), "t_start": t0, "t_end": t0 + length}
        tmp = out.with_suffix(".part.webm")
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
        for seg, off, dur in parts:
            cmd += ["-ss", f"{off:.3f}", "-t", f"{dur:.3f}", "-i", seg.path]
        if len(parts) == 1:
            cmd += ["-vf", f"scale=-2:{height}:flags=bicubic,fps=60,format=yuv420p"]
        else:
            w = height * 16 // 9
            chains = "".join(
                f"[{i}:v]scale={w}:{height}:force_original_aspect_ratio=decrease:flags=bicubic,"
                f"pad={w}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=60,format=yuv420p[p{i}];"
                for i in range(len(parts)))
            ins = "".join(f"[p{i}]" for i in range(len(parts)))
            cmd += ["-filter_complex", f"{chains}{ins}concat=n={len(parts)}:v=1:a=0[o]", "-map", "[o]"]
        cmd += ["-an", "-c:v", vp9_encoder(), "-deadline", "realtime", "-cpu-used", "8", "-row-mt", "1",
                "-tile-columns", "2", "-threads", "4", "-b:v", "3M", "-maxrate", "4M", "-bufsize", "6M",
                "-f", "webm", str(tmp)]
        result = run(cmd, 300)
        if result.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            tmp.unlink(missing_ok=True)
            raise MediaError("生成片段失败：" + _stderr(result))
        tmp.replace(out)
        return {"path": str(out), "t_start": t0, "t_end": t0 + length}

    # ---- 整局保存成一个文件 ----
    def export_range(self, t0: float, t1: float, out: Path) -> Path:
        """把 [t0, t1) 这一段原样拼成一个 MKV 文件：不重新编码，几秒钟就好，画质和原片一样。
        切点对齐到关键帧，开头可能多出 1~2 秒。"""
        parts = self.pieces(t0, t1)
        if not parts:
            raise MediaError("这一局的原片已清理，不能保存")
        out.parent.mkdir(parents=True, exist_ok=True)
        lines = ["ffconcat version 1.0"]
        for seg, off, dur in parts:
            lines.append("file '" + Path(seg.path).as_posix().replace("'", "'\\''") + "'")
            if off > 0.05:
                lines.append(f"inpoint {off:.3f}")
            if off + dur < seg.duration - 0.05:
                lines.append(f"outpoint {off + dur:.3f}")
        lst = out.with_name(out.stem + ".list.txt")
        tmp = out.with_name(out.stem + ".part.mkv")
        lst.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
            result = run([_need_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                          "-i", str(lst), "-map", "0", "-c", "copy", "-avoid_negative_ts", "make_zero", str(tmp)], 900)
        finally:
            lst.unlink(missing_ok=True)
        if result.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            tmp.unlink(missing_ok=True)
            raise MediaError("保存失败：" + _stderr(result))
        tmp.replace(out)
        return out

    # ---- 按分段增量处理（边录边做：每录完一段就处理这一段） ----
    def thumbs_dir(self) -> Path:
        return session_cache_dir("thumbs", self.session)

    def _sample(self, seg: Segment, every: float, post: str, out_pattern: Path, quality: int) -> list[float]:
        """每 every 秒取一帧关键帧（只解关键帧，很快），返回取到的每一帧在这一段里的准确时间。
        不用 fps 滤镜：它在很短的分段里会一帧都不出。"""
        vf = (f"select='isnan(prev_selected_t)+gte(floor(t/{every})-floor(prev_selected_t/{every}),1)',"
              f"showinfo,{post}")
        result = run([_need_ffmpeg(), "-hide_banner", "-nostats", "-loglevel", "info", "-skip_frame", "nokey",
                      "-i", seg.path, "-vf", vf, "-fps_mode", "passthrough", "-q:v", str(quality), str(out_pattern)], 900)
        if result.returncode != 0:
            log.warning("截取画面失败 %s：%s", seg.path, _stderr(result))
        text = result.stderr.decode("utf-8", "replace")
        return [float(x) for x in re.findall(r"pts_time:\s*(-?[\d.]+)", text)]

    @staticmethod
    def _key(seg: Segment) -> str:
        return f"{Path(seg.path).name}|{seg.size}"

    @staticmethod
    def _read_index(path: Path) -> dict:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def thumbs_for(self, seg: Segment, step: int = 10, width: int = 320) -> list[float]:
        """这一段的缩略图（已经做过就直接用），返回每张图在段内的时间。"""
        with _session_lock("thumbs", self.session):
            return self._thumbs_for(seg, step, width)

    def _thumbs_for(self, seg: Segment, step: int, width: int) -> list[float]:
        tdir = self.thumbs_dir()
        index = self._read_index(tdir / "index.json")
        segs = index.setdefault("segments", {})
        entry = segs.get(self._key(seg))
        stem = Path(seg.path).stem
        if entry and index.get("step") == step and all((tdir / f"{stem}_{i + 1:04d}.jpg").exists()
                                                       for i in range(len(entry))):
            return entry
        for old in tdir.glob(f"{stem}_*.jpg"):
            old.unlink(missing_ok=True)
        times = self._sample(seg, step, f"scale={width}:-2", tdir / f"{stem}_%04d.jpg", 6)
        images = sorted(tdir.glob(f"{stem}_*.jpg"))
        if len(times) != len(images):  # 读不到准确时间时按间隔估算
            times = [i * step for i in range(len(images))]
        index = self._read_index(tdir / "index.json")  # 重新读一次，别覆盖别的线程刚写的
        if index.get("step") != step:
            index = {"step": step, "segments": {}}
        index.setdefault("segments", {})[self._key(seg)] = times
        (tdir / "index.json").write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
        return times

    def make_thumbs(self, step: int = 10, width: int = 320, progress=None, cancelled=None) -> list[dict]:
        _need_ffmpeg()
        for k, seg in enumerate(self.segments):
            if cancelled and cancelled():
                break
            self.thumbs_for(seg, step, width)
            if progress:
                progress((k + 1) / max(1, len(self.segments)))
        return self.thumbs()[0]

    def thumbs(self) -> tuple[list[dict], int]:
        """已经生成的缩略图（按整场时间排好）。还没处理的分段没有缩略图。"""
        tdir = self.thumbs_dir()
        index = self._read_index(tdir / "index.json")
        step = int(index.get("step") or 10)
        segs = index.get("segments") or {}
        out = []
        for seg in self.segments:
            times = segs.get(self._key(seg))
            if not times:
                continue
            stem = Path(seg.path).stem
            for i, t in enumerate(times):
                if 0 <= t < seg.duration:
                    out.append({"t": round(seg.start + t, 2), "path": str(tdir / f"{stem}_{i + 1:04d}.jpg")})
        return out, step

    def has_thumbs_for(self, seg: Segment) -> bool:
        index = self._read_index(self.thumbs_dir() / "index.json")
        return self._key(seg) in (index.get("segments") or {})

    def sheets_for(self, seg: Segment, every: float = 3.0, cols: int = 3, rows: int = 3,
                   tile_width: int = 480) -> list[dict]:
        """这一段每隔 every 秒取一帧，每 cols×rows 帧拼一张图（发给 AI 用，左上角的 UID 水印会遮掉）。
        返回 [{"path": 图片, "times": [每格对应的整场时间]}]。"""
        with _session_lock("sheets", self.session):
            return self._sheets_for(seg, every, cols, rows, tile_width)

    def _sheets_for(self, seg: Segment, every: float, cols: int, rows: int, tile_width: int) -> list[dict]:
        out_dir = session_cache_dir("media", self.session) / f"scan_{int(every * 10)}_{cols}x{rows}_{tile_width}"
        out_dir.mkdir(parents=True, exist_ok=True)
        per = cols * rows
        index_path = out_dir / "index.json"
        index = self._read_index(index_path)
        stem = Path(seg.path).stem
        entry = index.get(self._key(seg))
        if not entry or not all((out_dir / f"{stem}_{j + 1:04d}.jpg").exists() for j in range(len(entry))):
            for old in out_dir.glob(f"{stem}_*.jpg"):
                old.unlink(missing_ok=True)
            times = self._sample(seg, every, f"{AI_MASK},scale={tile_width}:-2,tile={cols}x{rows}",
                                 out_dir / f"{stem}_%04d.jpg", 4)
            images = sorted(out_dir.glob(f"{stem}_*.jpg"))
            entry = [[t for t in times[j * per:(j + 1) * per] if 0 <= t < seg.duration] for j in range(len(images))]
            index = self._read_index(index_path)
            index[self._key(seg)] = entry
            index_path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
        return [{"path": str(out_dir / f"{stem}_{j + 1:04d}.jpg"), "times": [round(seg.start + t, 2) for t in chunk]}
                for j, chunk in enumerate(entry) if chunk]

    def frames_for(self, seg: Segment, every: float = 3.0, width: int = 1280) -> list[tuple[float, bytes]]:
        """这一段每隔 every 秒取一帧完整画面（遮掉 UID 水印），给本机文字识别用。
        和 sheets_for 用同样的取帧规则，所以时间一一对应；临时图片用完就删。"""
        tmp = session_cache_dir("media", self.session) / f"ocr_{Path(seg.path).stem}"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            times = self._sample(seg, every, f"{AI_MASK},scale={width}:-2", tmp / "f_%04d.jpg", 3)
            images = sorted(tmp.glob("f_*.jpg"))
            if len(times) != len(images):
                times = [i * every for i in range(len(images))]
            return [(round(seg.start + t, 2), img.read_bytes()) for t, img in zip(times, images) if 0 <= t < seg.duration]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def scan_sheets(self, every: float = 3.0, cols: int = 3, rows: int = 3, tile_width: int = 480,
                    progress=None, cancelled=None) -> list[dict]:
        """整场的拼图（所有分段）。"""
        _need_ffmpeg()
        sheets = []
        for k, seg in enumerate(self.segments):
            if cancelled and cancelled():
                break
            sheets += self.sheets_for(seg, every, cols, rows, tile_width)
            if progress:
                progress((k + 1) / max(1, len(self.segments)))
        return sheets

    # ---- 截帧 ----
    def frame(self, t: float, width: int = 960, mask: bool = False) -> bytes:
        loc = self.locate(t)
        if not loc:
            return b""
        seg, off = loc
        vf = (f"{AI_MASK}," if mask else "") + f"scale={width}:-2"
        out = run([_need_ffmpeg(), "-hide_banner", "-loglevel", "error", "-ss", f"{off:.3f}", "-i", seg.path,
                   "-frames:v", "1", "-vf", vf, "-q:v", "3", "-f", "image2pipe", "-vcodec", "mjpeg", "-"], 60)
        return out.stdout if out.returncode == 0 else b""

    def grid(self, times: list[float], cols: int, tile_width: int = 480, mask: bool = True) -> bytes:
        """把多个时刻的画面拼成一张宫格图（从左到右、从上到下），每格 16:9。发给 AI 时遮掉 UID 水印。"""
        if not times or not self.segments:
            return b""
        ffmpeg = _need_ffmpeg()
        w = tile_width - tile_width % 2
        h = (w * 9 // 16) - (w * 9 // 16) % 2
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error"]
        for t in times:
            seg, off = self.locate(t)
            cmd += ["-ss", f"{off:.3f}", "-i", seg.path]
        n = len(times)
        pre = f"{AI_MASK}," if mask else ""
        chains = "".join(
            f"[{i}:v]trim=end_frame=1,{pre}scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p[v{i}];" for i in range(n))
        if n == 1:
            filt = chains.rstrip(";").replace("[v0]", "[g]")
        else:
            layout = "|".join(f"{(i % cols) * w}_{(i // cols) * h}" for i in range(n))
            filt = chains + "".join(f"[v{i}]" for i in range(n)) + f"xstack=inputs={n}:layout={layout}:fill=black[g]"
        cmd += ["-filter_complex", filt, "-map", "[g]", "-frames:v", "1", "-q:v", "3",
                "-f", "image2pipe", "-vcodec", "mjpeg", "-"]
        out = run(cmd, 120)
        if out.returncode != 0:
            log.warning("拼图失败：%s", _stderr(out))
            return b""
        return out.stdout


_VP9: str | None = None


def vp9_encoder() -> str:
    """FFmpeg 里有 libvpx-vp9 就用 VP9，否则退回 VP8（两者网页组件都能播放）。"""
    global _VP9
    if _VP9 is None:
        ffmpeg = find_ffmpeg()
        names = run([ffmpeg, "-hide_banner", "-encoders"], 30).stdout.decode("utf-8", "replace") if ffmpeg else ""
        _VP9 = "libvpx-vp9" if "libvpx-vp9" in names else "libvpx"
    return _VP9


def file_url(path: str) -> str:
    return Path(path).resolve().as_uri()
