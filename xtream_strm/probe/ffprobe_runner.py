"""Locating and running ffprobe (returns its parsed JSON; mapping is in ffprobe_mapper).

ffprobe is always given the stream URL read from the ``.strm`` file – never the
``.strm`` path itself – with bounded probe size, analyse duration, network timeout and
a hard process timeout, so a dead stream cannot block the job. Only ``-show_streams``
and ``-show_format`` are used; frames and packets are never decoded.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Optional

from ..utils.redact import redact

log = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"
FFPROBE_NAME = "ffprobe.exe" if IS_WINDOWS else "ffprobe"


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
