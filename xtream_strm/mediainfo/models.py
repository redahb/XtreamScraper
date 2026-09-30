"""Normalized media-information model.

These are internal concepts, not XML element names. ``None`` always means "unknown":
booleans are tri-state (``True`` / ``False`` / ``None``) so an explicit ``False`` from a
source is kept apart from information that was never supplied.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Optional

MEDIA_SCHEMA_VERSION = 2

#: Kodi stream flags that may appear in ``<flags><flag>``.
APPROVED_FLAGS = frozenset({
    "comment", "default", "dub", "forced", "hearingimpaired", "karaoke", "lyrics",
    "original", "stillimages", "visualimpaired", "webvttdatapackets",
})
#: Kodi ``<hdrtype>`` values.
HDR_TYPES = ("dolbyvision", "hdr10plus", "hlg", "hdr10")
#: Jellyfin/Emby ``<format3d>`` values.
FORMAT_3D = frozenset({"FSBS", "FTAB", "HSBS", "HTAB", "MVC"})
SCAN_TYPES = frozenset({"progressive", "interlaced"})


@dataclass
class VideoStreamInfo:
    codec: Optional[str] = None
    bitrate: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    aspect: Optional[float] = None
    aspect_ratio_text: Optional[str] = None
    frame_rate: Optional[float] = None
    language: Optional[str] = None
    scan_type: Optional[str] = None
    default: Optional[bool] = None
    forced: Optional[bool] = None
    original: Optional[bool] = None
    duration_seconds: Optional[int] = None
    stereo_mode: Optional[str] = None
    format_3d: Optional[str] = None
    hdr_type: Optional[str] = None
    hdr_detail: Optional[str] = None


@dataclass
class AudioStreamInfo:
    codec: Optional[str] = None
    bitrate: Optional[int] = None
    language: Optional[str] = None
    scan_type: Optional[str] = None
    channels: Optional[int] = None
    sampling_rate: Optional[int] = None
    default: Optional[bool] = None
    forced: Optional[bool] = None
    original: Optional[bool] = None
    flags: list[str] = field(default_factory=list)


@dataclass
class SubtitleStreamInfo:
    codec: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    language: Optional[str] = None
    scan_type: Optional[str] = None
    default: Optional[bool] = None
    forced: Optional[bool] = None
    original: Optional[bool] = None
    flags: list[str] = field(default_factory=list)


def _has_value(stream: Any) -> bool:
    return any(value not in (None, [], "") for value in asdict(stream).values())


@dataclass
class MediaInfo:
    video_streams: list[VideoStreamInfo] = field(default_factory=list)
    audio_streams: list[AudioStreamInfo] = field(default_factory=list)
    subtitle_streams: list[SubtitleStreamInfo] = field(default_factory=list)
    duration_seconds: Optional[int] = None

    def has_useful_data(self) -> bool:
        return any(_has_value(s) for s in (*self.video_streams, *self.audio_streams, *self.subtitle_streams))

    # -- persistence (probe state) ------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps({"schema": MEDIA_SCHEMA_VERSION, **self.to_dict()}, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MediaInfo":
        def build(model, items):
            names = {f.name for f in fields(model)}
            return [model(**{k: v for k, v in item.items() if k in names})
                    for item in items or [] if isinstance(item, dict)]

        return cls(
            video_streams=build(VideoStreamInfo, data.get("video_streams")),
            audio_streams=build(AudioStreamInfo, data.get("audio_streams")),
            subtitle_streams=build(SubtitleStreamInfo, data.get("subtitle_streams")),
            duration_seconds=data.get("duration_seconds"),
        )

    @classmethod
    def from_json(cls, text: Optional[str]) -> Optional["MediaInfo"]:
        if not text:
            return None
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("schema") != MEDIA_SCHEMA_VERSION:
            return None  # older schema: re-probe rather than guess
        return cls.from_dict(data)
