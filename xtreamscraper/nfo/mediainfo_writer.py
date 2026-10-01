"""Serializes :class:`MediaInfo` into ``<fileinfo><streamdetails>``.

This is the only ``<fileinfo>`` writer in the application. It emits the approved
Kodi / Jellyfin / Emby technical vocabulary only, in a fixed field order, and never
writes empty, zero or placeholder values. It knows nothing about ffprobe or any other
source.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Iterable, Optional

from ..mediainfo.models import AudioStreamInfo, MediaInfo, SubtitleStreamInfo, VideoStreamInfo
from ..mediainfo.normalize import (
    format_aspect,
    format_frame_rate,
    normalize_aspect_text,
    normalize_codec,
    normalize_flags,
    normalize_format_3d,
    normalize_hdr_type,
    normalize_language,
    normalize_scan_type,
    positive_float,
    positive_int,
)
from .document import NfoDocument
from .paths import NfoKind

#: Only playable items carry stream details (never tvshow.nfo or season.nfo).
STREAM_NFO_KINDS = (NfoKind.MOVIE, NfoKind.EPISODE)


def _text(parent: ET.Element, tag: str, value: Optional[str]) -> None:
    if value not in (None, ""):
        ET.SubElement(parent, tag).text = value


def _int(parent: ET.Element, tag: str, value: object) -> None:
    number = positive_int(value)
    if number is not None:
        _text(parent, tag, str(number))


def _bool(parent: ET.Element, tag: str, value: Optional[bool]) -> None:
    if value is not None:
        _text(parent, tag, "True" if value else "False")


def _codec(parent: ET.Element, codec: Optional[str]) -> None:
    value = normalize_codec(codec)
    _text(parent, "codec", value)
    _text(parent, "micodec", value)


def _flags(parent: ET.Element, flags: Iterable[str]) -> None:
    approved = normalize_flags(flags)
    if approved:
        container = ET.SubElement(parent, "flags")
        for flag in approved:
            ET.SubElement(container, "flag").text = flag


def video_element(video: VideoStreamInfo, fallback_duration: Optional[int] = None) -> Optional[ET.Element]:
    el = ET.Element("video")
    _codec(el, video.codec)
    _int(el, "bitrate", video.bitrate)
    _int(el, "width", video.width)
    _int(el, "height", video.height)
    aspect = positive_float(video.aspect)
    _text(el, "aspect", format_aspect(aspect) if aspect else None)
    _text(el, "aspectratio", normalize_aspect_text(video.aspect_ratio_text))
    frame_rate = positive_float(video.frame_rate)
    _text(el, "framerate", format_frame_rate(frame_rate) if frame_rate else None)
    _text(el, "language", normalize_language(video.language))
    _text(el, "scantype", normalize_scan_type(video.scan_type))
    _bool(el, "default", video.default)
    _bool(el, "forced", video.forced)
    _bool(el, "original", video.original)
    seconds = positive_int(video.duration_seconds) or positive_int(fallback_duration)
    if seconds:
        _int(el, "duration", seconds // 60)  # whole minutes, truncated; omitted below one minute
        _text(el, "durationinseconds", str(seconds))
    _text(el, "stereomode", (video.stereo_mode or "").strip().lower() or None)
    _text(el, "format3d", normalize_format_3d(video.format_3d))
    _text(el, "hdrtype", normalize_hdr_type(video.hdr_type))
    _text(el, "hdrdetail", (video.hdr_detail or "").strip() or None)
    return el if len(el) else None


def audio_element(audio: AudioStreamInfo) -> Optional[ET.Element]:
    el = ET.Element("audio")
    _codec(el, audio.codec)
    _int(el, "bitrate", audio.bitrate)
    _text(el, "language", normalize_language(audio.language))
    _text(el, "scantype", normalize_scan_type(audio.scan_type))
    _int(el, "channels", audio.channels)
    _int(el, "samplingrate", audio.sampling_rate)
    _bool(el, "default", audio.default)
    _bool(el, "forced", audio.forced)
    _bool(el, "original", audio.original)
    _flags(el, audio.flags)
    return el if len(el) else None


def subtitle_element(subtitle: SubtitleStreamInfo) -> Optional[ET.Element]:
    el = ET.Element("subtitle")
    _codec(el, subtitle.codec)
    _int(el, "width", subtitle.width)
    _int(el, "height", subtitle.height)
    _text(el, "language", normalize_language(subtitle.language))
    _text(el, "scantype", normalize_scan_type(subtitle.scan_type))
    _bool(el, "default", subtitle.default)
    _bool(el, "forced", subtitle.forced)
    _bool(el, "original", subtitle.original)
    _flags(el, subtitle.flags)
    return el if len(el) else None


def streamdetails_element(media: MediaInfo) -> Optional[ET.Element]:
    """A complete ``<streamdetails>`` for ``media``, or ``None`` when nothing is known."""
    details = ET.Element("streamdetails")
    children = [video_element(v, media.duration_seconds) for v in media.video_streams]
    children += [audio_element(a) for a in media.audio_streams]
    children += [subtitle_element(s) for s in media.subtitle_streams]
    details.extend(c for c in children if c is not None)
    return details if len(details) else None


def apply_media_info(doc: NfoDocument, media: MediaInfo) -> bool:
    """Replace the document's ``<streamdetails>`` with one generated from ``media``.

    The existing ``<fileinfo>`` is reused (never duplicated) and its other children are
    kept; everything outside ``<fileinfo>`` is untouched. Returns ``False`` without
    changing anything when ``media`` holds no meaningful stream information.
    """
    if doc.kind not in STREAM_NFO_KINDS:
        raise ValueError(f"Stream details belong in movie or episode NFOs, not <{doc.root.tag}>")
    details = streamdetails_element(media)
    if details is None:
        return False
    fileinfo = doc.find_or_create("fileinfo")
    doc.replace_elements("streamdetails", [details], parent=fileinfo)
    return True
