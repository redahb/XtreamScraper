"""The ``<fileinfo><streamdetails>`` section, owned by media probing.

Only ``<video>`` and ``<audio>`` inside ``<streamdetails>`` are replaced, and only when
new data for them was detected; other children (e.g. ``<subtitle>``) and everything
outside ``<fileinfo>`` stay as they are.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Optional

from ..probe.models import AudioStream, MediaInfo, VideoStream
from .document import NfoDocument


def _add(parent: ET.Element, tag: str, value: object) -> None:
    child = ET.SubElement(parent, tag)
    child.text = str(value)


def video_element(video: VideoStream, duration_seconds: Optional[int]) -> Optional[ET.Element]:
    element = ET.Element("video")
    if video.codec:
        _add(element, "codec", video.codec)
    if video.width and video.width > 0:
        _add(element, "width", int(video.width))
    if video.height and video.height > 0:
        _add(element, "height", int(video.height))
    if video.aspect and video.aspect > 0:
        _add(element, "aspect", f"{video.aspect:.2f}")
    if duration_seconds and duration_seconds > 0:
        _add(element, "durationinseconds", int(duration_seconds))
    return element if len(element) else None


def audio_element(audio: AudioStream) -> Optional[ET.Element]:
    element = ET.Element("audio")
    if audio.codec:
        _add(element, "codec", audio.codec)
    if audio.channels and audio.channels > 0:
        _add(element, "channels", int(audio.channels))
    return element if len(element) else None


def apply_media_info(doc: NfoDocument, media: MediaInfo) -> bool:
    """Write probe results into ``doc``. Returns False when there was nothing valid to write."""
    video = media.primary_video
    audio = media.primary_audio
    video_el = video_element(video, media.duration_seconds) if video else None
    audio_el = audio_element(audio) if audio else None
    if video_el is None and audio_el is None:
        return False
    streamdetails = doc.find_or_create("fileinfo/streamdetails")
    if video_el is not None:
        doc.replace_elements("video", [video_el], parent=streamdetails)
    if audio_el is not None:
        doc.replace_elements("audio", [audio_el], parent=streamdetails)
    return True


def read_media_summary(doc: NfoDocument) -> dict[str, str]:
    """Flat view of what the NFO currently says (for diagnostics and tests)."""
    result: dict[str, str] = {}
    for section in ("video", "audio"):
        node = doc.find(f"fileinfo/streamdetails/{section}")
        if node is None:
            continue
        for child in node:
            result[f"{section}.{child.tag}"] = child.text or ""
    return result
