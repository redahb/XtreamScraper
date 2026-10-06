"""Bounded HTTP retries shared by the artwork downloader and scraper plugin clients.

Each client keeps its own session, timeouts, error classes and messages; this module only
owns the loop they all need:

* exponential backoff (``backoff * 2 ** (attempt - 1)``) with an injectable ``sleep``,
* retries for transport failures (``requests.RequestException``, timeouts included) and for
  configurable statuses (429 and 5xx by default),
* ``Retry-After`` (seconds or an HTTP date) for the statuses that honour it, capped and never
  raising, whatever the server sends,
* a hook that turns every other response into the client's result or its own exception, and
  a hook that builds the client's exception when the retries are exhausted.

It never names a plugin and never logs URLs; callers pass an already redacted ``label``.
"""

from __future__ import annotations

import email.utils
import logging
import math
import time
from datetime import timezone
from typing import Any, Callable, Collection, Final, Literal, Optional, TypeVar

import requests

from .. import APP_NAME, __version__

log = logging.getLogger(__name__)

T = TypeVar("T")

#: Statuses retried by default: rate limiting and server errors.
RETRY_STATUSES: Final[frozenset[int]] = frozenset({429, *range(500, 600)})
#: Statuses whose ``Retry-After`` header is honoured by default.
RETRY_AFTER_STATUSES: Final[frozenset[int]] = frozenset({429})
DEFAULT_MAX_RETRY_AFTER: Final = 30.0

#: When a response is closed: never (the caller owns it), only when it is discarded for a
#: retry, or always once the attempt is over (the hook must have read what it needs).
CloseMode = Literal["never", "retried", "always"]
CLOSE_NEVER: Final = "never"
CLOSE_RETRIED: Final = "retried"
CLOSE_ALWAYS: Final = "always"


def user_agent() -> str:
    """The application's ``User-Agent`` header value."""
    return f"{APP_NAME}/{__version__}"


def retry_after_seconds(response: Any, cap: float = DEFAULT_MAX_RETRY_AFTER,
                        now: Callable[[], float] = time.time) -> float:
    """Seconds to wait according to the response's ``Retry-After`` header, within ``[0, cap]``.

    Accepts delta-seconds and HTTP dates (a date without a zone is taken as GMT, as HTTP
    requires). A missing, empty or unparseable value gives ``0.0``; it never raises.

    Contract:
        Silences:
            - Any exception from ``response.headers.get`` (an odd headers object), and parse
              or overflow errors from the HTTP date, all give ``0.0``. NaN gives ``0.0``,
              ``inf`` gives ``cap``. A ``0.0`` result cannot be told apart from "no header".
    """
    headers = getattr(response, "headers", None) or {}
    try:
        value = headers.get("Retry-After")
    except Exception:  # an odd headers object must not break the retry loop
        return 0.0
    if value is None:
        return 0.0
    text = str(value).strip()
    if not text:
        return 0.0
    try:
        seconds = float(text)
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError, OverflowError):
            return 0.0
        if parsed is None:
            return 0.0
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        try:
            seconds = parsed.timestamp() - now()
        except (OverflowError, OSError, ValueError):
            return 0.0
    if math.isnan(seconds):
        return 0.0
    return min(cap, max(0.0, seconds))


def _close(response: Any) -> None:
    try:
        response.close()
    except Exception:  # pragma: no cover - a fake or broken response without close()
        pass


def request_with_retries(
    send: Callable[[], Any],
    *,
    retries: int,
    backoff: float,
    sleep: Callable[[float], None],
    on_response: Callable[[Any], T],
    transport_problem: Callable[[requests.RequestException], str],
    status_problem: Callable[[int], str],
    give_up: Callable[[str, int], BaseException],
    retry_statuses: Collection[int] = RETRY_STATUSES,
    retry_after_statuses: Collection[int] = RETRY_AFTER_STATUSES,
    max_retry_after: float = DEFAULT_MAX_RETRY_AFTER,
    close: CloseMode = CLOSE_NEVER,
    logger: Optional[logging.Logger] = None,
    label: str = "",
) -> T:
    """Call ``send()`` until it yields a final response, then return ``on_response(response)``.

    * ``send`` performs one request (and any per-attempt bookkeeping, e.g. a request counter
      or a concurrency gate) and returns the response.
    * A response whose status is in ``retry_statuses`` is retried; its problem text comes from
      ``status_problem(status)``. For ``retry_after_statuses`` the wait is at least the
      ``Retry-After`` value, capped at ``max_retry_after``.
    * Any other response goes to ``on_response``, which returns the result or raises the
      client's own exception (never retried). ``requests.RequestException`` raised by ``send``
      or by ``on_response`` (e.g. while streaming a body) is a transport failure, described by
      ``transport_problem(exc)`` and retried.
    * ``retries`` is the number of retries after the first attempt; when they are exhausted
      ``give_up(problem, attempts)`` builds the exception that is raised.

    Contract:
        Preconditions:
            - ``retries`` of 0 or less means a single attempt (no retry, no sleep).
            - ``backoff`` should be >= 0. The wait before retry ``n`` is
              ``backoff * 2 ** (n - 1)`` with no upper bound (10 retries at ``backoff=1``
              wait 1023 s in total); only the ``Retry-After`` part is capped.
            - ``send()`` must return an object with ``status_code``; otherwise the
              ``AttributeError`` propagates (implicit, not retried).

        Raises:
            BaseException: Whatever ``give_up(problem, attempts)`` returns, once
                ``attempts > retries``. It is raised outside the ``except`` block, so it carries
                no ``__context__``.
            Exception: Anything ``send()`` or ``on_response()`` raises that is not a
                ``requests.RequestException`` propagates at once (no retry). Exceptions from
                ``status_problem`` and ``transport_problem`` propagate too.

        Silences:
            - Every ``requests.RequestException`` from ``send()`` or ``on_response()`` until
              the retries run out; only the last problem reaches ``give_up``. The ones before
              are logged at DEBUG with ``label`` (never the URL).
            - ``close`` errors from a fake or broken response (``_close``).
    """
    logger = logger or log
    attempt = 0
    while True:
        attempt += 1
        wait = backoff * (2 ** (attempt - 1))
        response = None
        try:
            response = send()
            status = response.status_code
            if status not in retry_statuses:
                return on_response(response)
        except requests.RequestException as exc:
            problem = transport_problem(exc)
            if close == CLOSE_RETRIED and response is not None:  # on_response failed mid-body
                _close(response)
        else:
            problem = status_problem(status)
            if status in retry_after_statuses:
                wait = max(wait, retry_after_seconds(response, max_retry_after))
            if close == CLOSE_RETRIED:
                _close(response)
        finally:
            if close == CLOSE_ALWAYS and response is not None:
                _close(response)
        if attempt > retries:
            raise give_up(problem, attempt)
        if label:
            logger.debug("%s: %s; retrying in %.1fs (attempt %d)", label, problem, wait, attempt)
        else:
            logger.debug("%s; retrying in %.1fs (attempt %d)", problem, wait, attempt)
        sleep(wait)
