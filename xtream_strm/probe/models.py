"""Technical media information model.

Stored as JSON in the probe state (with :data:`MEDIA_SCHEMA_VERSION`) and rendered into
NFO ``<fileinfo>`` by :mod:`xtream_strm.nfo.fileinfo`. Optional fields beyond the
initial NFO set (bitrate, frame rate, HDR, languages, subtitles...) are already captured
so that later versions can write them without re-probing.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Optional

MEDIA_SCHEMA_VERSION = 1


@dataclass
class VideoStream:
    codec: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    aspect: Optional[float] = None
    profile: Optional[str] = None
    bitrate: Optional[int] = None
    frame_rate: Optional[float] = None
    bit_depth: Optional[int] = None
    pixel_format: Optional[str] = None
    scan_type: Optional[str] = None
    color_space: Optional[str] = None
    color_transfer: Optional[str] = None
    color_primaries: Optional[str] = None
    hdr_type: Optional[str] = None
    dolby_vision: bool = False
    language: Optional[str] = None
    default: bool = False


@dataclass
class AudioStream:
    codec: Optional[str] = None
    channels: Optional[int] = None
    channel_layout: Optional[str] = None
    sample_rate: Optional[int] = None
    bitrate: Optional[int] = None
    language: Optional[str] = None
    default: bool = False


@dataclass
class SubtitleStream:
    codec: Optional[str] = None
    language: Optional[str] = None
    forced: bool = False
    default: bool = False


@dataclass
class MediaInfo:
    container: Optional[str] = None
    duration_seconds: Optional[int] = None
    bitrate: Optional[int] = None
    video: list[VideoStream] = field(default_factory=list)
    audio: list[AudioStream] = field(default_factory=list)
    subtitles: list[SubtitleStream] = field(default_factory=list)

    @property
    def primary_video(self) -> Optional[VideoStream]:
        for stream in self.video:
            if stream.default:
                return stream
        return self.video[0] if self.video else None

    @property
    def primary_audio(self) -> Optional[AudioStream]:
        for stream in self.audio:
            if stream.default:
                return stream
        return self.audio[0] if self.audio else None

    def has_useful_data(self) -> bool:
        video, audio = self.primary_video, self.primary_audio
        return bool(
            (video and (video.codec or video.width or video.height))
            or (audio and (audio.codec or audio.channels))
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps({"schema": MEDIA_SCHEMA_VERSION, **self.to_dict()}, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MediaInfo":
        def build(model, items):
            names = {f.name for f in fields(model)}
            return [model(**{k: v for k, v in item.items() if k in names}) for item in items or [] if isinstance(item, dict)]

        return cls(
            container=data.get("container"),
            duration_seconds=data.get("duration_seconds"),
            bitrate=data.get("bitrate"),
            video=build(VideoStream, data.get("video")),
            audio=build(AudioStream, data.get("audio")),
            subtitles=build(SubtitleStream, data.get("subtitles")),
        )

    @classmethod
    def from_json(cls, text: Optional[str]) -> Optional["MediaInfo"]:
        if not text:
            return None
        try:
            return cls.from_dict(json.loads(text))
        except (TypeError, ValueError):
            return None
