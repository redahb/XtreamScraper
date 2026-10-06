"""Xtream player_api client for VOD and Series.

Only VOD and Series actions are implemented. Live TV actions are intentionally absent.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .. import APP_NAME, __version__
from ..utils.redact import redact
from .models import AccountInfo, Category, SeriesDetails, SeriesEntry, VodStream
from .parsing import (
    ParsedRecords,
    T,
    is_error_object,
    parse_account,
    parse_categories,
    parse_series_info,
    parse_series_list,
    parse_vod_streams,
)

log = logging.getLogger(__name__)

RETRY_STATUSES = (429, 500, 502, 503, 504, 520, 521, 522, 524)


class XtreamError(Exception):
    """Any failure talking to a provider. Messages are always credential-free."""


class XtreamAuthError(XtreamError):
    pass


class XtreamResponseError(XtreamError):
    """The provider answered, but not with usable JSON or not in a known format."""


@dataclass
class ClientOptions:
    connect_timeout: float = 15.0
    read_timeout: float = 120.0
    retries: int = 3
    backoff_factor: float = 1.0
    request_delay_ms: int = 0


class XtreamClient:
    base_url: str
    username: str
    password: str
    options: ClientOptions
    session: requests.Session
    _delay_lock: threading.Lock
    _last_request: float  # time.monotonic() of the last request; 0.0 before the first

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        user_agent: str = "",
        options: Optional[ClientOptions] = None,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.options = options or ClientOptions()
        self.session = session or self._build_session()
        self.session.headers["User-Agent"] = user_agent.strip() or f"{APP_NAME}/{__version__}"
        self._delay_lock = threading.Lock()
        self._last_request = 0.0

    def _build_session(self) -> requests.Session:
        """A session that retries GETs at the transport level (urllib3 ``Retry``).

        Contract:
            Preconditions:
                - Needs urllib3 2.x: the ``backoff_max`` keyword does not exist in 1.x and
                  raises ``TypeError`` there (implicit; ``requests`` does not pin urllib3).
            Silences:
                - Retryable statuses (``RETRY_STATUSES``) are retried up to
                  ``options.retries`` times; with ``raise_on_status=False`` the last
                  response is returned, so ``_get_json`` still sees the final status.
        """
        retry = Retry(
            total=self.options.retries,
            connect=self.options.retries,
            read=self.options.retries,
            status=self.options.retries,
            backoff_factor=self.options.backoff_factor,
            backoff_max=60,
            status_forcelist=RETRY_STATUSES,
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        session = requests.Session()
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "XtreamClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- transport --------------------------------------------------------------------------
    def _throttle(self) -> None:
        """Wait until ``options.request_delay_ms`` has passed since the previous request.

        Contract:
            Preconditions:
                - A delay of 0 or less disables throttling. The sleep happens while
                  ``_delay_lock`` is held, so threads sharing one client are serialised.
        """
        delay = self.options.request_delay_ms / 1000.0
        if delay <= 0:
            return
        with self._delay_lock:
            wait = self._last_request + delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    def _redact(self, text: object) -> str:
        return redact(text, (self.username, self.password))

    def _get_json(self, action: Optional[str] = None, **params: Any) -> Any:
        """GET ``player_api.php`` (``action`` None = authenticate) and decode the JSON body.

        Returns the decoded JSON value, or ``None`` for an empty or whitespace-only body
        (several panels answer that for "nothing here"). Parameters whose value is None
        are left out of the query.

        Contract:
            Raises:
                XtreamError: transport failure after the urllib3 retries
                    (``requests.RequestException``), or HTTP status >= 400 other than
                    401/403. The message is redacted with this client's credentials.
                XtreamAuthError: HTTP 401 or 403.
                XtreamResponseError: the body is not valid JSON (including a number past
                    Python's int-digit limit); the message carries a redacted 120-byte sample.
            Silences:
                - An empty body becomes ``None``, which the parsers treat as an empty list;
                  the caller cannot tell it apart from ``null``.
                - Invalid UTF-8 is decoded with replacement characters before parsing.
        """
        query: dict[str, Any] = {"username": self.username, "password": self.password}
        if action:
            query["action"] = action
        query.update({k: v for k, v in params.items() if v is not None})
        url = f"{self.base_url}/player_api.php"
        label = action or "authenticate"
        self._throttle()
        try:
            response = self.session.get(
                url, params=query, timeout=(self.options.connect_timeout, self.options.read_timeout)
            )
        except requests.RequestException as exc:
            raise XtreamError(f"{label}: request failed: {self._redact(exc)}") from None
        if response.status_code in (401, 403):
            raise XtreamAuthError(f"{label}: provider refused access (HTTP {response.status_code})")
        if response.status_code >= 400:
            raise XtreamError(f"{label}: HTTP {response.status_code} from provider")
        body = response.content
        if not body or not body.strip():
            # Several panels answer an empty body for "nothing here".
            return None
        text = body.decode("utf-8-sig", errors="replace")  # cannot raise: errors="replace"
        try:
            return json.loads(text)
        except ValueError:  # JSONDecodeError, and the plain ValueError of the int-digit limit
            sample = self._redact(body[:120].decode("utf-8", errors="replace"))
            raise XtreamResponseError(f"{label}: provider returned invalid or truncated JSON: {sample!r}") from None

    def _get_list(
        self,
        action: str,
        parse: Callable[[Any, Optional[list[str]]], ParsedRecords[T]],
        warnings: Optional[list[str]],
        **params: Any,
    ) -> ParsedRecords[T]:
        """Fetch and parse a list endpoint; an error object answered with HTTP 200 raises.

        Contract:
            Raises:
                XtreamError / XtreamAuthError / XtreamResponseError: from ``_get_json``.
                XtreamResponseError: the answer is a non-empty object from which no record
                    (parsed or listed-but-unparsed) could be read (``is_error_object``), e.g.
                    ``{"user_info": {"auth": 0}}``. Only its first five keys, redacted,
                    are in the message.
            Silences:
                - Malformed records are skipped and reported in ``warnings`` (see
                  ``parsing._parse_all``); IDs still readable land in ``unparsed_ids``.
        """
        data = self._get_json(action, **params)
        records = parse(data, warnings)
        if is_error_object(data, records):
            keys = ", ".join(sorted(str(k) for k in data)[:5])
            raise XtreamResponseError(
                f"{action}: provider answered an object without records (an error or an unknown format; "
                f"keys: {self._redact(keys)})"
            )
        return records

    # -- API ----------------------------------------------------------------------------------
    def authenticate(self) -> AccountInfo:
        """Account state from the bare ``player_api.php`` call.

        Contract:
            Raises:
                XtreamError / XtreamAuthError / XtreamResponseError: from ``_get_json``.
            Silences:
                - A wrong shape (not an object, no ``user_info``) is not an error: it comes
                  back as ``AccountInfo(authenticated=False)`` with an explanatory message.
        """
        return parse_account(self._get_json())

    def get_vod_categories(self, warnings: Optional[list[str]] = None) -> ParsedRecords[Category]:
        return self._get_list("get_vod_categories", parse_categories, warnings)

    def get_series_categories(self, warnings: Optional[list[str]] = None) -> ParsedRecords[Category]:
        return self._get_list("get_series_categories", parse_categories, warnings)

    def get_vod_streams(self, category_id: str, warnings: Optional[list[str]] = None) -> ParsedRecords[VodStream]:
        return self._get_list("get_vod_streams", parse_vod_streams, warnings, category_id=category_id)

    def get_series(self, category_id: str, warnings: Optional[list[str]] = None) -> ParsedRecords[SeriesEntry]:
        return self._get_list("get_series", parse_series_list, warnings, category_id=category_id)

    def get_series_info(self, series_id: str) -> SeriesDetails:
        """Seasons and episodes of one series.

        Contract:
            Raises:
                XtreamError / XtreamAuthError / XtreamResponseError: from ``_get_json``.
            Silences:
                - Unlike the list endpoints, an answer that is not an object (``[]``, empty
                  body) is "no episodes", reported only in ``SeriesDetails.warnings``; an
                  error object yields no episodes either. Callers must not treat an empty
                  episode list as absence (the sync does not).
        """
        return parse_series_info(series_id, self._get_json("get_series_info", series_id=series_id))
