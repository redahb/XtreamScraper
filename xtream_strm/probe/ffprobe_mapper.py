"""ffprobe JSON -> normalized :class:`MediaInfo`.

The only ffprobe-specific interpretation in the application. It fills the core model and
nothing else; how that model becomes XML is decided by :mod:`xtream_strm.nfo`.
Values are taken only when ffprobe gives clear evidence; everything else stays unset.
"""

from __future__ import annotations

from typing import Any, Optional

from ..mediainfo.models import AudioStreamInfo, MediaInfo, SubtitleStreamInfo, VideoStreamInfo
from ..mediainfo.normalize import (
    normalize_aspect_text,
    normalize_codec,
    normalize_flags,
    normalize_language,
    parse_ratio,
    positive_float,
    positive_int,
    tri_state,
)

#: ffprobe disposition -> Kodi stream flag. Dispositions not listed have no Kodi flag.
DISPOSITION_FLAGS = {
    "comment": "comment",
    "default": "default",
    "dub": "dub",
    "forced": "forced",
    "hearing_impaired": "hearingimpaired",
    "karaoke": "karaoke",
    "lyrics": "lyrics",
    "original": "original",
    "still_image": "stillimages",
    "visual_impaired": "visualimpaired",
}
_INTERLACED_FIELD_ORDERS = {"tt", "bb", "tb", "bt"}


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _disposition(stream: dict, key: str) -> Optional[bool]:
    """Known only when ffprobe supplied the disposition object and the key in it."""
    disposition = stream.get("disposition")
    if not isinstance(disposition, dict) or key not in disposition:
        return None
    return tri_state(disposition.get(key))


def _flags(stream: dict) -> list[str]:
    disposition = _dict(stream.get("disposition"))
    return normalize_flags(flag for key, flag in DISPOSITION_FLAGS.items() if tri_state(disposition.get(key)))


def _language(stream: dict) -> Optional[str]:
    tags = _dict(stream.get("tags"))
    return normalize_language(tags.get("language") or tags.get("LANGUAGE"))


def _side_data(stream: dict) -> list[dict]:
    return [s for s in stream.get("side_data_list") or [] if isinstance(s, dict)]


def _duration(stream: dict, format_duration: Optional[float]) -> Optional[int]:
    """Stream duration, then container duration, then duration_ts * time_base."""
    seconds = positive_float(stream.get("duration")) or format_duration
    if not seconds:
        ticks = positive_int(stream.get("duration_ts"))
        base = parse_ratio(stream.get("time_base"))
        if ticks and base:
            seconds = float(ticks * base)
    return positive_int(seconds) if seconds else None


def _aspect(stream: dict, width: Optional[int], height: Optional[int]) -> Optional[float]:
    dar = parse_ratio(stream.get("display_aspect_ratio"))
    if dar:
        return round(float(dar), 4)
    if not (width and height):
        return None
    raw_sar = stream.get("sample_aspect_ratio")
    sar = parse_ratio(raw_sar)
    if sar:
        return round(width / height * float(sar), 4)
    if raw_sar in (None, ""):
        return round(width / height, 4)  # no SAR given: pixels treated as square
    return None  # SAR present but unusable (e.g. 0:1): don't guess


def _frame_rate(stream: dict) -> Optional[float]:
    for key in ("avg_frame_rate", "r_frame_rate"):
        rate = parse_ratio(stream.get(key))
        if rate:
            return round(float(rate), 3)
    return None


def _scan_type(stream: dict) -> Optional[str]:
    order = str(stream.get("field_order") or "").strip().lower()
    if order == "progressive":
        return "progressive"
    if order in _INTERLACED_FIELD_ORDERS:
        return "interlaced"
    return None


def _hdr_type(stream: dict) -> Optional[str]:
    """Dolby Vision > HDR10+ > HLG > HDR10 > unset. Only positive evidence counts."""
    types = [str(s.get("side_data_type") or "").lower() for s in _side_data(stream)]
    if any("dovi" in t or "dolby vision" in t for t in types):
        return "dolbyvision"
    if any("2094-40" in t or "hdr10+" in t or "hdr10plus" in t for t in types):
        return "hdr10plus"
    transfer = str(stream.get("color_transfer") or "").strip().lower()
    if transfer == "arib-std-b67":
        return "hlg"
    if transfer == "smpte2084":
        return "hdr10"
    return None


def _stereo_mode(stream: dict) -> Optional[str]:
    """Kodi stereomode from Stereo 3D side data; only side-by-side and top/bottom packings."""
    for side in _side_data(stream):
        if str(side.get("side_data_type") or "").strip().lower() != "stereo 3d":
            continue
        packing = str(side.get("type") or "").strip().lower()
        inverted = tri_state(side.get("inverted")) is True
        if packing == "side by side":
            return "right_left" if inverted else "left_right"
        if packing == "top and bottom":
            return "bottom_top" if inverted else "top_bottom"
    return None


def _video(stream: dict, format_duration: Optional[float]) -> VideoStreamInfo:
    width, height = positive_int(stream.get("width")), positive_int(stream.get("height"))
    return VideoStreamInfo(
        codec=normalize_codec(stream.get("codec_name"), stream.get("codec_tag_string")),
        bitrate=positive_int(stream.get("bit_rate")),
        width=width,
        height=height,
        aspect=_aspect(stream, width, height),
        aspect_ratio_text=normalize_aspect_text(stream.get("display_aspect_ratio")),
        frame_rate=_frame_rate(stream),
        language=_language(stream),
        scan_type=_scan_type(stream),
        default=_disposition(stream, "default"),
        forced=_disposition(stream, "forced"),
        original=_disposition(stream, "original"),
        duration_seconds=_duration(stream, format_duration),
        stereo_mode=_stereo_mode(stream),
        format_3d=None,  # ffprobe cannot tell half from full packing
        hdr_type=_hdr_type(stream),
        hdr_detail=None,  # no verified ffprobe -> Kodi hdrdetail mapping yet
    )


def _audio(stream: dict) -> AudioStreamInfo:
    return AudioStreamInfo(
        codec=normalize_codec(stream.get("codec_name"), stream.get("codec_tag_string")),
        bitrate=positive_int(stream.get("bit_rate")),
        language=_language(stream),
        channels=positive_int(stream.get("channels")),
        sampling_rate=positive_int(stream.get("sample_rate")),
        default=_disposition(stream, "default"),
        forced=_disposition(stream, "forced"),
        original=_disposition(stream, "original"),
        flags=_flags(stream),
    )


def _subtitle(stream: dict) -> SubtitleStreamInfo:
    return SubtitleStreamInfo(
        codec=normalize_codec(stream.get("codec_name"), stream.get("codec_tag_string")),
        width=positive_int(stream.get("width")),
        height=positive_int(stream.get("height")),
        language=_language(stream),
        default=_disposition(stream, "default"),
        forced=_disposition(stream, "forced"),
        original=_disposition(stream, "original"),
        flags=_flags(stream),
    )


def map_ffprobe(data: Any) -> MediaInfo:
    """Convert ``ffprobe -show_streams -show_format -print_format json`` output."""
    data = _dict(data)
    format_duration = positive_float(_dict(data.get("format")).get("duration"))
    raw = [s for s in data.get("streams") or [] if isinstance(s, dict)]
    ordered = sorted(enumerate(raw), key=lambda p: (positive_int(p[1].get("index")) or 0, p[0]))
    media = MediaInfo()
    for _, stream in ordered:
        codec_type = stream.get("codec_type")
        if codec_type == "video":
            if _disposition(stream, "attached_pic"):
                continue  # embedded cover art, not playable video
            media.video_streams.append(_video(stream, format_duration))
        elif codec_type == "audio":
            media.audio_streams.append(_audio(stream))
        elif codec_type == "subtitle":
            media.subtitle_streams.append(_subtitle(stream))
    media.duration_seconds = next(
        (v.duration_seconds for v in media.video_streams if v.duration_seconds), None
    ) or (positive_int(format_duration) if format_duration else None)
    return media
