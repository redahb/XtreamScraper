"""Date-based versions: ``YYYYMMDD`` (the app and every plugin)."""

from __future__ import annotations

import datetime
import re

_DATE_VERSION = re.compile(r"^\d{8}$")


def is_date_version(value: object) -> bool:
    """True for an eight-digit ``YYYYMMDD`` string that is a real calendar date."""
    if not isinstance(value, str) or not _DATE_VERSION.match(value):
        return False
    try:
        datetime.datetime.strptime(value, "%Y%m%d")
    except ValueError:
        return False
    return True
