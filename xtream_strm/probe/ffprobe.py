"""Locating and running ffprobe, and turning its JSON into :class:`MediaInfo`.

ffprobe is always given the stream URL read from the ``.strm`` file – never the
``.strm`` path itself – with bounded probe size, analyse duration, network timeout and
a hard process timeout, so a dead stream cannot block the job.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Optional

from ..utils.redact import redact
from .models import AudioStream, MediaInfo, SubtitleStream, VideoStream

log = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"
FFPROBE_NAME = "ffprobe.exe" if IS_WINDOWS else "ffprobe"
_BIT_DEPTH_IN_PIX_FMT = re.compile(r"p(\d{2})(?:le|be)$")
_HMS = re.compile(r"^(\d+):(\d{1,2}):(\d{1,2}(?:\.\d+)?)$")


class ProbeError(Exception):
    """A probe attempt failed. The message is credential-free."""


@dataclass
class ProbeOptions:
    timeout_seconds: int = 60
    analyze_duration_ms: int = 5000
    probe_size_kb: int = 5000
    user_agent: str = ""


# -- discovery ------------------------------------------------------------------------------
def locate_ffprobe(configured: str = "", app_root: str = "") -> tuple[Optional[str], str]:
    """Return ``(path, how_found)``.

    Order: configured path > ``ffprobe(.exe)`` next to the application (also ``bin/`` and
    ``ffmpeg/bin/``) > system ``PATH``.
    """
    if configured:
        candidate = configured.strip().strip('"')
        if os.path.isdir(candidate):
            candidate = os.path.join(candidate, FFPROBE_NAME)
        if os.path.isfile(candidate):
            return candidate, "configured path"
        return None, f"configured path not found: {candidate}"
    if app_root:
        for sub in ("", "bin", os.path.join("ffmpeg", "bin"), "ffmpeg"):
            candidate = os.path.join(app_root, sub, FFPROBE_NAME)
            if os.path.isfile(candidate):
                return candidate, "application folder"
    found = shutil.which("ffprobe")
    if found:
        return found, "system PATH"
    return None, "ffprobe not found (not configured, not next to the application, not on PATH)"


def _creationflags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0


def ffprobe_version(path: str) -> str:
    try:
        result = subprocess.run(
            [path, "-hide_banner", "-version"], capture_output=True, timeout=15, creationflags=_creationflags()
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProbeError(f"Could not run ffprobe: {exc}") from None
    if result.returncode != 0:
        raise ProbeError(f"ffprobe -version exited with code {result.returncode}")
    first = result.stdout.decode("utf-8", errors="replace").splitlines()
    return first[0].strip() if first else "ffprobe (unknown version)"


# -- running ------------------------------------------------------------------------------------
def build_command(ffprobe: str, url: str, options: ProbeOptions) -> list[str]:
    cmd = [
        ffprobe,
        "-v", "error",
        "-hide_banner",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        "-analyzeduration", str(int(options.analyze_duration_ms) * 1000),
        "-probesize", str(int(options.probe_size_kb) * 1024),
    ]
    if url.lower().startswith(("http://", "https://")):
        # Network read/connect timeout in microseconds; stops ffprobe hanging on a dead host.
        cmd += ["-rw_timeout", str(max(5, int(options.timeout_seconds) // 2) * 1_000_000)]
        if options.user_agent:
            cmd += ["-user_agent", options.user_agent]
    cmd += ["-i", url]
    return cmd


def run_ffprobe(ffprobe: str, url: str, options: ProbeOptions) -> dict[str, Any]:
    cmd = build_command(ffprobe, url, options)
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=options.timeout_seconds, creationflags=_creationflags()
        )
    except subprocess.TimeoutExpired:
        raise ProbeError(f"ffprobe timed out after {options.timeout_seconds}s") from None
    except OSError as exc:
        raise ProbeError(f"Could not start ffprobe: {exc}") from None
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
        detail = stderr[-1] if stderr else "no error output"
        raise ProbeError(f"ffprobe exited with code {result.returncode}: {redact(detail)}"[:500])
    try:
        return json.loads(result.stdout.decode("utf-8", errors="replace") or "{}")
    except ValueError:
        raise ProbeError("ffprobe returned invalid JSON") from None


# -- parsing ------------------------------------------------------------------------------------
def _int(value: Any) -> Optional[int]:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _ratio(value: Any) -> Optional[Fraction]:
    if not value or not isinstance(value, str) or ":" not in value and "/" not in value:
        return None
    left, _, right = value.replace("/", ":").partition(":")
    try:
        numerator, denominator = int(left), int(right)
    except ValueError:
        return None
    if numerator <= 0 or denominator <= 0:
        return None
    return Fraction(numerator, denominator)


def _duration(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    match = _HMS.match(text)
    try:
        seconds = (int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))) if match else float(text)
    except ValueError:
        return None
    return seconds if seconds > 0 else None


def _aspect(stream: dict[str, Any], width: Optional[int], height: Optional[int]) -> Optional[float]:
    dar = _ratio(stream.get("display_aspect_ratio"))
    if dar:
        return round(float(dar), 2)
    if width and height:
        sar = _ratio(stream.get("sample_aspect_ratio")) or Fraction(1, 1)
        return round(float(Fraction(width, height) * sar), 2)
    return None


def _language(stream: dict[str, Any]) -> Optional[str]:
    tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
    lang = tags.get("language") or tags.get("LANGUAGE")
    return lang if lang and lang.lower() not in ("und", "unknown") else None


def _disposition(stream: dict[str, Any], key: str) -> bool:
    disposition = stream.get("disposition") if isinstance(stream.get("disposition"), dict) else {}
    return bool(_int(disposition.get(key)))


def _hdr(stream: dict[str, Any]) -> tuple[Optional[str], bool]:
    side_data = stream.get("side_data_list") if isinstance(stream.get("side_data_list"), list) else []
    dolby = any("DOVI" in str(s.get("side_data_type", "")).upper() or "DOLBY VISION" in str(s.get("side_data_type", "")).upper()
                for s in side_data if isinstance(s, dict))
    transfer = str(stream.get("color_transfer") or "").lower()
    hdr = None
    if transfer == "smpte2084":
        hdr = "HDR10"
    elif transfer == "arib-std-b67":
        hdr = "HLG"
    if dolby:
        hdr = "Dolby Vision" if hdr is None else f"Dolby Vision / {hdr}"
    return hdr, dolby


def parse_ffprobe_output(data: dict[str, Any]) -> MediaInfo:
    fmt = data.get("format") if isinstance(data.get("format"), dict) else {}
    streams = [s for s in data.get("streams") or [] if isinstance(s, dict)]
    media = MediaInfo(
        container=(fmt.get("format_name") or None),
        bitrate=_int(fmt.get("bit_rate")),
    )
    duration = _duration(fmt.get("duration"))
    for stream in streams:
        codec_type = stream.get("codec_type")
        codec = (stream.get("codec_name") or "").lower() or None
        if codec_type == "video":
            if _disposition(stream, "attached_pic"):
                continue  # cover art, not the movie
            width, height = _int(stream.get("width")), _int(stream.get("height"))
            pix_fmt = stream.get("pix_fmt") or None
            depth = _int(stream.get("bits_per_raw_sample"))
            if depth is None and pix_fmt:
                match = _BIT_DEPTH_IN_PIX_FMT.search(pix_fmt)
                depth = int(match.group(1)) if match else (8 if pix_fmt.startswith(("yuv420p", "yuvj420p", "nv12")) else None)
            frame_rate = _ratio(stream.get("avg_frame_rate")) or _ratio(stream.get("r_frame_rate"))
            field_order = str(stream.get("field_order") or "").lower()
            hdr, dolby = _hdr(stream)
            media.video.append(VideoStream(
                codec=codec,
                width=width,
                height=height,
                aspect=_aspect(stream, width, height),
                profile=stream.get("profile") or None,
                bitrate=_int(stream.get("bit_rate")),
                frame_rate=round(float(frame_rate), 3) if frame_rate else None,
                bit_depth=depth,
                pixel_format=pix_fmt,
                scan_type=("progressive" if field_order == "progressive"
                           else "interlaced" if field_order in ("tt", "bb", "tb", "bt") else None),
                color_space=stream.get("color_space") or None,
                color_transfer=stream.get("color_transfer") or None,
                color_primaries=stream.get("color_primaries") or None,
                hdr_type=hdr,
                dolby_vision=dolby,
                language=_language(stream),
                default=_disposition(stream, "default"),
            ))
            if duration is None:
                duration = _duration(stream.get("duration")) or _duration((stream.get("tags") or {}).get("DURATION"))
        elif codec_type == "audio":
            media.audio.append(AudioStream(
                codec=codec,
                channels=_int(stream.get("channels")),
                channel_layout=stream.get("channel_layout") or None,
                sample_rate=_int(stream.get("sample_rate")),
                bitrate=_int(stream.get("bit_rate")),
                language=_language(stream),
                default=_disposition(stream, "default"),
            ))
        elif codec_type == "subtitle":
            media.subtitles.append(SubtitleStream(
                codec=codec,
                language=_language(stream),
                forced=_disposition(stream, "forced"),
                default=_disposition(stream, "default"),
            ))
    media.duration_seconds = int(round(duration)) if duration else None
    return media
