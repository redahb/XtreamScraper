"""Xtream base-URL normalisation and stream-URL construction."""

from __future__ import annotations

import re
from urllib.parse import quote, urlsplit, urlunsplit

# Characters that may stay raw inside a credential path segment. Anything else
# (``/``, ``?``, ``#``, ``%``, whitespace, non-ASCII) is percent-encoded so the URL stays
# well-formed; for ordinary credentials this is a no-op, matching the raw URLs panels expect.
_SAFE_SEGMENT = "!$&'()*+,;=:@-._~"
_EXTENSION = re.compile(r"^[A-Za-z0-9]{1,6}$")
_API_SUFFIXES = ("/player_api.php", "/get.php", "/panel_api.php", "/xmltv.php")

DEFAULT_MOVIE_EXTENSION = "mp4"
DEFAULT_EPISODE_EXTENSION = "mkv"


def normalize_base_url(raw: str) -> str:
    """``host:8080/player_api.php?x=y`` -> ``http://host:8080``.

    Contract:
        Silences:
            - Never validates: query and fragment are dropped, the scheme is lowercased,
              the host part is kept as typed. ``user:pass@`` in the netloc is kept too.
            - Blank input gives ``""``. Raises ``ValueError`` only for a netloc
              ``urlsplit`` rejects (e.g. an unbalanced IPv6 bracket; implicit).
    """
    value = (raw or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = "http://" + value
    parts = urlsplit(value)
    path = parts.path.rstrip("/")
    lowered = path.lower()
    for suffix in _API_SUFFIXES:
        if lowered.endswith(suffix):
            path = path[: -len(suffix)]
            break
    return urlunsplit((parts.scheme.lower(), parts.netloc, path.rstrip("/"), "", ""))


def clean_extension(value: object, default: str) -> str:
    ext = str(value or "").strip().lstrip(".")
    return ext.lower() if _EXTENSION.match(ext) else default


def _segment(value: str) -> str:
    return quote(str(value), safe=_SAFE_SEGMENT)


def build_movie_url(base_url: str, username: str, password: str, stream_id: str, extension: str) -> str:
    """``<base>/movie/<user>/<pass>/<id>.<ext>`` with each segment percent-encoded.

    Contract:
        Silences:
            - An extension that is not 1-6 ASCII alphanumerics becomes ``mp4``.
    """
    ext = clean_extension(extension, DEFAULT_MOVIE_EXTENSION)
    return f"{base_url.rstrip('/')}/movie/{_segment(username)}/{_segment(password)}/{_segment(stream_id)}.{ext}"


def build_episode_url(base_url: str, username: str, password: str, episode_id: str, extension: str) -> str:
    ext = clean_extension(extension, DEFAULT_EPISODE_EXTENSION)
    return f"{base_url.rstrip('/')}/series/{_segment(username)}/{_segment(password)}/{_segment(episode_id)}.{ext}"


def stream_id_from_url(url: str) -> str | None:
    """The item ID at the end of a movie/series stream URL (``.../1234.mkv`` -> ``1234``).

    Contract:
        Silences:
            - Returns None for a URL ``urlsplit`` rejects or one ending in ``/``.
            - Does not percent-decode: for an ID the builders encode (e.g. ``"a b"`` ->
              ``a%20b``) the result differs from the original ID.
            - Only the last ``.ext`` is stripped; any URL (not only stream URLs) is accepted.
    """
    text = url.strip()
    try:
        path = urlsplit(text).path
    except ValueError:  # e.g. an unbalanced IPv6 bracket in the netloc
        return None
    last = path.rsplit("/", 1)[-1]
    if not last:
        return None
    return last.rsplit(".", 1)[0] if "." in last else last
