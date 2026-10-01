"""Xtream player_api client for VOD and Series.

Only VOD and Series actions are implemented. Live TV actions are intentionally absent.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .. import APP_NAME, __version__
from ..utils.redact import redact
from .models import AccountInfo, Category, SeriesDetails, SeriesEntry, VodStream
from .parsing import parse_account, parse_categories, parse_series_info, parse_series_list, parse_vod_streams

log = logging.getLogger(__name__)

RETRY_STATUSES = (429, 500, 502, 503, 504, 520, 521, 522, 524)


class XtreamError(Exception):
    """Any failure talking to a provider. Messages are always credential-free."""


class XtreamAuthError(XtreamError):
    pass


class XtreamResponseError(XtreamError):
    """The provider answered, but not with usable JSON."""


@dataclass
class ClientOptions:
    connect_timeout: float = 15.0
    read_timeout: float = 120.0
    retries: int = 3
    backoff_factor: float = 1.0
    request_delay_ms: int = 0


class XtreamClient:
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

    def __exit__(self, *exc) -> None:
        self.close()

    # -- transport --------------------------------------------------------------------------
    def _throttle(self) -> None:
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
        try:
            return json.loads(body.decode("utf-8-sig", errors="replace"))
        except ValueError:
            sample = self._redact(body[:120].decode("utf-8", errors="replace"))
            raise XtreamResponseError(f"{label}: provider returned invalid or truncated JSON: {sample!r}") from None

    # -- API ----------------------------------------------------------------------------------
    def authenticate(self) -> AccountInfo:
        return parse_account(self._get_json())

    def get_vod_categories(self, warnings: Optional[list[str]] = None) -> list[Category]:
        return parse_categories(self._get_json("get_vod_categories"), warnings)

    def get_series_categories(self, warnings: Optional[list[str]] = None) -> list[Category]:
        return parse_categories(self._get_json("get_series_categories"), warnings)

    def get_vod_streams(self, category_id: str, warnings: Optional[list[str]] = None) -> list[VodStream]:
        return parse_vod_streams(self._get_json("get_vod_streams", category_id=category_id), warnings)

    def get_series(self, category_id: str, warnings: Optional[list[str]] = None) -> list[SeriesEntry]:
        return parse_series_list(self._get_json("get_series", category_id=category_id), warnings)

    def get_series_info(self, series_id: str) -> SeriesDetails:
        return parse_series_info(series_id, self._get_json("get_series_info", series_id=series_id))
