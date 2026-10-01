"""Parses ``<fileinfo><streamdetails>`` from an NFO into :class:`MediaInfo`.

Independent of any media source. Every ``<video>``, ``<audio>`` and ``<subtitle>`` is
read; unknown elements are ignored and a malformed field only loses that field.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Optional, Union

from ..mediainfo.models import AudioStreamInfo, MediaInfo, SubtitleStreamInfo, VideoStreamInfo
from ..mediainfo.normalize import (
    normalize_aspect_text,
    normalize_codec,
    normalize_flags,
    normalize_format_3d,
    normalize_hdr_type,
    normalize_language,
    normalize_scan_type,
    parse_ratio,
    positive_int,
    tri_state,
)
from .document import NfoDocument


def _t(el: ET.Element, tag: str) -> Optional[str]:
    child = el.find(tag)
    if child is None or child.text is None:
        return None
    return child.text.strip() or None


def _codec(el: ET.Element) -> Optional[str]:
    return normalize_codec(_t(el, "codec") or _t(el, "micodec"))


def _flags(el: ET.Element) -> list[str]:
    return normalize_flags(f.text or "" for f in el.findall("flags/flag"))


def _duration(el: ET.Element) -> Optional[int]:
    seconds = positive_int(_t(el, "durationinseconds"))
    if seconds:
        return seconds
    minutes = positive_int(_t(el, "duration"))
    return minutes * 60 if minutes else None


def parse_video(el: ET.Element) -> VideoStreamInfo:
    aspect = parse_ratio(_t(el, "aspect"))
    frame_rate = parse_ratio(_t(el, "framerate"))
    return VideoStreamInfo(
        codec=_codec(el),
        bitrate=positive_int(_t(el, "bitrate")),
        width=positive_int(_t(el, "width")),
        height=positive_int(_t(el, "height")),
        aspect=round(float(aspect), 4) if aspect else None,
        aspect_ratio_text=normalize_aspect_text(_t(el, "aspectratio")),
        frame_rate=round(float(frame_rate), 3) if frame_rate else None,
        language=normalize_language(_t(el, "language")),
        scan_type=normalize_scan_type(_t(el, "scantype")),
        default=tri_state(_t(el, "default")),
        forced=tri_state(_t(el, "forced")),
        original=tri_state(_t(el, "original")),
        duration_seconds=_duration(el),
        stereo_mode=(_t(el, "stereomode") or "").lower() or None,
        format_3d=normalize_format_3d(_t(el, "format3d")),
        hdr_type=normalize_hdr_type(_t(el, "hdrtype")),
        hdr_detail=_t(el, "hdrdetail"),
    )


def parse_audio(el: ET.Element) -> AudioStreamInfo:
    return AudioStreamInfo(
        codec=_codec(el),
        bitrate=positive_int(_t(el, "bitrate")),
        language=normalize_language(_t(el, "language")),
        scan_type=normalize_scan_type(_t(el, "scantype")),
        channels=positive_int(_t(el, "channels")),
        sampling_rate=positive_int(_t(el, "samplingrate")),
        default=tri_state(_t(el, "default")),
        forced=tri_state(_t(el, "forced")),
        original=tri_state(_t(el, "original")),
        flags=_flags(el),
    )


def parse_subtitle(el: ET.Element) -> SubtitleStreamInfo:
    return SubtitleStreamInfo(
        codec=_codec(el),
        width=positive_int(_t(el, "width")),
        height=positive_int(_t(el, "height")),
        language=normalize_language(_t(el, "language")),
        scan_type=normalize_scan_type(_t(el, "scantype")),
        default=tri_state(_t(el, "default")),
        forced=tri_state(_t(el, "forced")),
        original=tri_state(_t(el, "original")),
        flags=_flags(el),
    )


def parse_media_info(source: Union[NfoDocument, ET.Element]) -> MediaInfo:
    """Media information of an NFO document (or its root element). Never raises for content."""
    root = source.root if isinstance(source, NfoDocument) else source
    media = MediaInfo()
    for details in root.findall("fileinfo/streamdetails"):
        media.video_streams += [parse_video(v) for v in details.findall("video")]
        media.audio_streams += [parse_audio(a) for a in details.findall("audio")]
        media.subtitle_streams += [parse_subtitle(s) for s in details.findall("subtitle")]
    media.duration_seconds = next((v.duration_seconds for v in media.video_streams if v.duration_seconds), None)
    return media
