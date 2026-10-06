"""When does an item need (re-)probing?"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from ..mediainfo.models import MEDIA_SCHEMA_VERSION
from ..storage.probe_state import STATUS_OK, ProbeRecord
from ..utils.timeutil import older_than, utcnow


@dataclass(frozen=True)
class ProbePolicy:
    retry_failed_after_hours: int = 24
    max_failed_attempts: int = 5
    stale_days: int = 0  # 0 = successful probes never go stale


def needs_probe(
    record: Optional[ProbeRecord],
    url_hash: str,
    policy: ProbePolicy,
    force: bool = False,
    now: Optional[datetime] = None,
) -> tuple[bool, str]:
    """Return ``(should_probe, reason)``.

    Contract:
        Preconditions:
            - ``url_hash`` must be computed like ``record.url_hash`` (``url_fingerprint``);
              any difference counts as a changed URL and probes again.
        Silences:
            - A missing or unparseable ``last_attempt_at`` counts as old enough to retry; a
              successful record without ``last_success_at`` is treated like a failed one.
    """
    now = now or utcnow()
    if force:
        return True, "forced"
    if record is None or record.status == "never":
        return True, "new"
    if record.url_hash != url_hash:
        return True, "stream URL changed"
    if record.status == STATUS_OK and record.last_success_at:
        if record.media_schema != MEDIA_SCHEMA_VERSION:
            return True, "media-info format upgraded"
        if policy.stale_days > 0 and older_than(record.last_success_at, timedelta(days=policy.stale_days), now):
            return True, "stale"
        return False, "unchanged"
    # Previous attempt failed.
    if record.failed_attempts >= policy.max_failed_attempts:
        return False, "too many failed attempts (use force re-probe)"
    if not older_than(record.last_attempt_at, timedelta(hours=policy.retry_failed_after_hours), now):
        return False, "waiting before retrying failed probe"
    return True, "retry after failure"
