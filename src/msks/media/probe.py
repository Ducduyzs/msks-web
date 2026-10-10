"""Probe và trích xuất bằng FFmpeg (mục 5.1).

Mọi lệnh chạy với danh sách tham số (không qua shell), timeout, và chỉ đọc file local — media processor
không tự mở URL. Timeline chuẩn: millisecond nguyên tính từ `start_time` của container, khoảng nửa mở.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..errors import PermanentError

ALLOWED_CONTAINERS = {"mov,mp4,m4a,3gp,3g2,mj2", "matroska,webm"}


def _tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise PermanentError("ffmpeg_missing", f"Không tìm thấy {name} trên máy worker.")
    return path


def run(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise PermanentError("media_timeout", f"{Path(args[0]).name} vượt thời hạn {int(timeout)} s.") from exc


@dataclass(frozen=True)
class MediaInfo:
    duration_ms: int
    start_time_ms: int
    container: str
    has_audio: bool
    has_video: bool
    width: int | None
    height: int | None
    video_codec: str | None
    audio_codec: str | None
    audio_language: str | None

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def probe(path: Path, timeout: float = 120) -> MediaInfo:
    result = run(
        [_tool("ffprobe"), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        timeout,
    )
    if result.returncode != 0:
        raise PermanentError("media_unreadable", "Không đọc được file video (ffprobe thất bại).")
    data = json.loads(result.stdout.decode("utf-8", "replace") or "{}")
    fmt = data.get("format") or {}
    container = fmt.get("format_name", "")
    if container not in ALLOWED_CONTAINERS:
        raise PermanentError("unsupported_media_type", f"Định dạng {container or 'không rõ'} chưa hỗ trợ (MVP: MP4/WebM).")
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    try:
        duration = float(fmt.get("duration") or 0)
    except ValueError:
        duration = 0.0
    if duration <= 0:
        raise PermanentError("media_unreadable", "Không xác định được thời lượng video.")
    return MediaInfo(
        duration_ms=int(round(duration * 1000)),
        start_time_ms=int(round(float(fmt.get("start_time") or 0) * 1000)),
        container=container,
        has_audio=audio is not None,
        has_video=video is not None,
        width=int(video["width"]) if video and video.get("width") else None,
        height=int(video["height"]) if video and video.get("height") else None,
        video_codec=video.get("codec_name") if video else None,
        audio_codec=audio.get("codec_name") if audio else None,
        audio_language=((audio or {}).get("tags") or {}).get("language"),
    )


def duration_ms(path: Path, timeout: float = 60) -> int:
    result = run([_tool("ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)], timeout)
    try:
        return int(round(float(json.loads(result.stdout)["format"]["duration"]) * 1000))
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        raise PermanentError("media_unreadable", "Không đọc được thời lượng đoạn âm thanh.") from exc


def extract_audio(source: Path, folder: Path, chunk_seconds: int, timeout: float) -> tuple[Path, list[tuple[Path, int, int]]]:
    """Âm thanh 16 kHz mono: một WAV cho ASR + các đoạn FLAC (lossless) làm bằng chứng.

    Trả (wav, [(flac, start_ms, end_ms)]). Mốc đoạn tính bằng tổng thời lượng thực của các đoạn trước,
    không giả định đoạn cắt đúng `chunk_seconds`.
    """
    wav = folder / "audio.wav"
    result = run([_tool("ffmpeg"), "-nostdin", "-v", "error", "-y", "-i", str(source), "-map", "0:a:0", "-vn",
                  "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)], timeout)
    if result.returncode != 0 or not wav.exists():
        raise PermanentError("audio_extract_failed", "Không tách được âm thanh từ video.")
    pattern = folder / "audio_%03d.flac"
    result = run([_tool("ffmpeg"), "-nostdin", "-v", "error", "-y", "-i", str(wav), "-c:a", "flac",
                  "-f", "segment", "-segment_time", str(chunk_seconds), "-reset_timestamps", "1", str(pattern)], timeout)
    if result.returncode != 0:
        raise PermanentError("audio_extract_failed", "Không chia được âm thanh thành đoạn.")
    chunks, cursor = [], 0
    for path in sorted(folder.glob("audio_*.flac")):
        length = duration_ms(path)
        chunks.append((path, cursor, cursor + length))
        cursor += length
    return wav, chunks


def sample_frames(source: Path, folder: Path, interval_seconds: float, max_width: int, timeout: float,
                  start_time_ms: int = 0) -> list[tuple[Path, int]]:
    """Lấy frame thật cách nhau ≥ `interval_seconds` và đọc **timestamp thực** của từng frame (showinfo).

    Không suy timestamp từ chỉ số frame: bộ lọc fps lấy frame cuối trong mỗi ô thời gian (lệch tới một
    khoảng lấy mẫu, đo 09/10) và video VFR không có FPS cố định (mục 5.1). Trả [(jpg, timestamp_ms)]
    trên timeline chuẩn (trừ start_time của container).
    """
    pattern = folder / "sample_%06d.jpg"
    vf = (f"select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,{interval_seconds})',showinfo,"
          f"scale='min({max_width},iw)':-2")
    result = run([_tool("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "info", "-y", "-i", str(source),
                  "-map", "0:v:0", "-an", "-vf", vf, "-fps_mode", "vfr", "-q:v", "3", str(pattern)], timeout)
    if result.returncode != 0:
        raise PermanentError("frame_extract_failed", "Không lấy được khung hình từ video.")
    log = result.stderr.decode("utf-8", "replace")
    times = [float(t) for t in re.findall(r"\[Parsed_showinfo[^\]]*\].*?pts_time:\s*(-?[0-9.]+)", log)]
    frames = sorted(folder.glob("sample_*.jpg"))
    if len(times) != len(frames):
        raise PermanentError("frame_timestamp_mismatch", "Không đọc được timestamp của các khung hình đã lấy.")
    return [(path, max(0, int(round(seconds * 1000)) - start_time_ms)) for path, seconds in zip(frames, times)]
