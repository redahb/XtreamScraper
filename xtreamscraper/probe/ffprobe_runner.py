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
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Optional

from ..utils.redact import redact

log = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"
FFPROBE_NAME = "ffprobe.exe" if IS_WINDOWS else "ffprobe"


#: The only executable names a configured ffprobe path may end in (compared case-insensitively).
FFPROBE_FILE_NAMES = ("ffprobe", "ffprobe.exe")


def _is_network_path(value: str) -> bool:
    """UNC and device paths: ``\\\\server\\share``, ``//server/share``, ``\\\\?\\UNC\\...``, ``\\??\\...``."""
    return (len(value) >= 2 and value[0] in "\\/" and value[1] in "\\/") or value.startswith("\\??\\")


def ffprobe_path_problem(path: str) -> Optional[str]:
    """Why a configured ffprobe path is refused, or ``None`` when it is acceptable.

    A network path is refused before the file system is touched (opening one would connect to
    that server). Otherwise the path must name ``ffprobe``/``ffprobe.exe`` or a folder (which
    then must contain it), so the setting cannot run an arbitrary program.

    Contract:
        Preconditions:
            - A folder is accepted as is (``os.path.isdir`` touches the local file system);
              :func:`locate_ffprobe` then appends the platform's ``ffprobe`` name.
        Silences:
            - A blank path is acceptable (``None``): it means "not configured".
    """
    value = (path or "").strip().strip('"')
    if not value:
        return None
    if _is_network_path(value):
        return "network (UNC) paths are not allowed for ffprobe; use a folder on a local drive"
    if value[-1] in "\\/" or os.path.isdir(value):
        return None
    name = re.split(r"[\\/]", value)[-1]
    if name.lower() not in FFPROBE_FILE_NAMES:
        return f"the ffprobe path must end in ffprobe.exe (or be the folder that contains it), not '{name}'"
    return None


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

    Order: configured path > ``ffprobe(.exe)`` next to the application (also ``bin/``,
    ``ffmpeg/bin/`` and ``ffmpeg/``) > system ``PATH``.

    Contract:
        Silences:
            - A configured path that is refused or not found gives ``(None, reason)`` and does
              not fall back to the other locations. The application folder and ``PATH`` are
              trusted without the name check.
    """
    if configured:
        candidate = configured.strip().strip('"')
        if _is_network_path(candidate):  # checked before any file system access
            return None, f"configured path refused: {ffprobe_path_problem(candidate)}"
        if os.path.isdir(candidate):
            candidate = os.path.join(candidate, FFPROBE_NAME)
        if os.path.isfile(candidate):
            problem = ffprobe_path_problem(candidate)
            if problem:
                return None, f"configured path refused: {problem}"
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
    """The first line of ``ffprobe -version``.

    Contract:
        Raises:
            ProbeError: ``path`` fails :func:`ffprobe_path_problem`, cannot be started, takes
                more than 15 s or exits non-zero.
    """
    problem = ffprobe_path_problem(path)
    if problem:
        raise ProbeError(f"Refusing to run {path}: {problem}")
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
    """Run ffprobe on ``url`` and return its parsed JSON output.

    Contract:
        Preconditions:
            - ``ffprobe`` must come from :func:`locate_ffprobe`; this function does not repeat
              the name check.
        Raises:
            ProbeError: Timeout, start failure, a non-zero exit (with the redacted last
                stderr line, at most 500 characters) or invalid JSON.
        Silences:
            - Valid JSON that is not an object (a list, a number) is returned as is despite
              the annotation; :func:`map_ffprobe` treats it as empty. Undecodable output
              bytes are replaced before parsing; empty output gives ``{}``.
    """
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
