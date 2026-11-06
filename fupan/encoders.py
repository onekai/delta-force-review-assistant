"""FFmpeg 录制命令。

画面来源是 ddagrab（Windows 桌面复制接口，系统级截屏，不碰游戏进程），裁剪到游戏窗口；
编码用显卡硬件编码器，按显卡品牌排优先级，首次运行时逐个自检，选第一个能用的。
"""

from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# 每个编码器：需要的滤镜 + 编码参数（码率参数另外拼）
_ENCODER_SPECS: dict[str, dict] = {
    "h264_nvenc": {"vf": [], "args": ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-bf", "0"]},
    "hevc_nvenc": {"vf": [], "args": ["-c:v", "hevc_nvenc", "-preset", "p4", "-rc", "vbr", "-bf", "0"]},
    # AMF 不接受截屏出来的 BGRA 显存画面，先拷回内存转成 NV12（和 h264_mf 一样的路子），再交给 AMD 硬件编码
    "h264_amf": {"vf": ["hwdownload", "format=bgra", "format=nv12"],
                 "args": ["-c:v", "h264_amf", "-usage", "transcoding", "-quality", "balanced", "-rc", "vbr_peak"]},
    "h264_qsv": {"vf": ["hwmap=derive_device=qsv", "format=qsv"], "args": ["-c:v", "h264_qsv", "-preset", "medium"]},
    "h264_mf": {"vf": ["hwdownload", "format=bgra", "format=nv12"], "args": ["-c:v", "h264_mf", "-hw_encoding", "1"]},
}

VENDOR_ORDER = {
    "nvidia": ["h264_nvenc", "hevc_nvenc", "h264_mf"],
    "amd": ["h264_amf", "h264_mf"],
    "intel": ["h264_qsv", "h264_mf"],
}
FALLBACK_ORDER = ["h264_nvenc", "h264_amf", "h264_qsv", "h264_mf"]


def known_encoders() -> list[str]:
    return list(_ENCODER_SPECS)


def candidate_order(gpu_names: list[str]) -> list[str]:
    order: list[str] = []
    for name in gpu_names:
        low = name.lower()
        vendor = "nvidia" if ("nvidia" in low or "geforce" in low or "rtx" in low) else \
                 "amd" if ("amd" in low or "radeon" in low) else \
                 "intel" if ("intel" in low or "arc" in low or "iris" in low or "uhd" in low) else ""
        for enc in VENDOR_ORDER.get(vendor, []):
            if enc not in order:
                order.append(enc)
    for enc in FALLBACK_ORDER:
        if enc not in order:
            order.append(enc)
    return order


# 编码器规格变了就让已缓存的自检结果失效，下次启动重新检测
ENCODER_SPEC_VERSION = 2


def auto_bitrate_mbps(height: int) -> int:
    """复盘看画面足够清楚的码率：1080p 16、1440p 24、4K 40 Mbps（一局 15 分钟约 1.8～4.5 GB）。"""
    if height <= 1080:
        return 16
    if height <= 1440:
        return 24
    return 40


def find_ffmpeg() -> str:
    exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    local = paths.app_root() / "bin" / exe
    if local.exists():
        return str(local)
    from shutil import which
    return which(exe) or ""


@dataclass(frozen=True)
class Crop:
    x: int
    y: int
    width: int
    height: int


def even(value: int) -> int:
    return max(2, value - value % 2)


def ddagrab_source(output_idx: int, fps: int, crop: Crop | None) -> str:
    opts = [f"output_idx={output_idx}", f"framerate={fps}", "draw_mouse=0"]
    if crop is not None:
        opts += [f"offset_x={crop.x}", f"offset_y={crop.y}", f"video_size={even(crop.width)}x{even(crop.height)}"]
    return "ddagrab=" + ":".join(opts)


def encode_args(encoder: str, fps: int, bitrate_mbps: int) -> tuple[list[str], list[str]]:
    spec = _ENCODER_SPECS[encoder]
    rate = [
        "-b:v", f"{bitrate_mbps}M",
        "-maxrate", f"{int(bitrate_mbps * 1.5)}M",
        "-bufsize", f"{bitrate_mbps * 2}M",
        "-g", str(fps * 2),
    ]
    return list(spec["vf"]), spec["args"] + rate


def probe_command(ffmpeg: str, encoder: str, output_idx: int = 0) -> list[str]:
    vf, args = encode_args(encoder, 30, 8)
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
           "-i", ddagrab_source(output_idx, 30, None), "-t", "1"]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    return cmd + args + ["-f", "null", "-"]


def record_command(ffmpeg: str, encoder: str, output_idx: int, crop: Crop, fps: int,
                   bitrate_mbps: int, segment_seconds: int, out_pattern: Path) -> list[str]:
    vf, args = encode_args(encoder, fps, bitrate_mbps)
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "info", "-nostats", "-progress", "pipe:1",
           "-f", "lavfi", "-i", ddagrab_source(output_idx, fps, crop)]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    cmd += args
    cmd += ["-f", "segment", "-segment_time", str(segment_seconds), "-reset_timestamps", "1",
            "-segment_format", "matroska", str(out_pattern)]
    return cmd


def probe(ffmpeg: str, encoder: str, output_idx: int = 0, timeout: float = 20.0) -> tuple[bool, str]:
    """实际编码 1 秒画面，能成功才算这个编码器可用。"""
    try:
        result = subprocess.run(probe_command(ffmpeg, encoder, output_idx), capture_output=True,
                                text=True, encoding="utf-8", errors="replace",
                                timeout=timeout, creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    ok = result.returncode == 0
    return ok, (result.stderr or "").strip()[-500:]


def select_encoder(ffmpeg: str, gpu_names: list[str], output_idx: int = 0) -> tuple[str, list[dict]]:
    """返回 (可用编码器名或空字符串, 每个候选的自检记录)。"""
    report: list[dict] = []
    for enc in candidate_order(gpu_names):
        ok, detail = probe(ffmpeg, enc, output_idx)
        report.append({"encoder": enc, "ok": ok, "detail": detail})
        log.info("编码器自检 %s：%s", enc, "可用" if ok else "不可用")
        if ok:
            return enc, report
    return "", report
