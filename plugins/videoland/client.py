"""Minimal Videoland HTTP client (anonymous web-client interfaces, no account).

* **Token:** layout and block requests need a short-lived anonymous bearer JWT from the token
  service. Its request is signed (see :func:`sign_request`) and carries the installation's device ID.
  The JWT is cached for :data:`JWT_LIFETIME` seconds (Videoland issues 30-minute tokens).
* **401/498:** Videoland answers a rejected or expired token with HTTP 498 ("Token
  expired/invalid"), and may use 401. A layout/block request answered that way drops the
  cached token, gets a new one and is retried exactly once; a second rejection is an
  :class:`VideolandAuthError`.
* **Session-wide failures:** once the token cannot be obtained, every further call raises the
  same error at once (no request), so a broken signature never costs one request per item.
* **Catalog pages** are public HTML; their data is read by :mod:`.parsing`.
* Connection/read timeouts; bounded retries with backoff for timeouts, connection errors, 429
  (honouring ``Retry-After``) and 5xx. Other statuses are never retried.
* At most :data:`MAX_CONCURRENT_REQUESTS` Videoland requests run at the same time, over all
  sessions (scrape jobs of different providers can run in parallel).

* **Client constants** (:data:`CLIENT_CONFIG`) were read from Videoland's browser bundle. They
  are not credentials and not user settings: they identify the web client towards Videoland's
  anonymous services. When Videoland ships a new frontend they can change; the token request
  then fails with :class:`VideolandClientConfigError` ("client settings outdated") and the values
  are updated here deliberately. Nothing crawls the bundle at run time.

Nothing here logs a token, an ``Authorization`` header or an ``X-Auth-Token`` value; obtained
tokens are also registered with :func:`~xtreamscraper.utils.redact.register_secret`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional
from urllib.parse import quote

import requests

from xtreamscraper.utils.http_retry import request_with_retries, user_agent
from xtreamscraper.utils.redact import register_secret

log = logging.getLogger(__name__)


# -- web-client constants ------------------------------------------------------------------------
@dataclass(frozen=True)
class ClientConfig:
    """Everything the plugin needs to talk to Videoland like its web client does."""

    #: Key parts of the anonymous token request signature (HMAC-SHA1 key = salt + sugar).
    salt: str
    sugar: str
    #: Appended to an image path before hashing it (plain SHA1) into the ``hash`` parameter.
    image_salt: str
    #: Layout API path segment.
    capacity_token: str
    customer: str
    platform: str
    section: str
    #: ``X-Client-Release``: the web client's release, not the application's version.
    client_release: str
    auth_base: str = "https://front-auth.videoland.bedrock.tech"
    layout_base: str = "https://layout.videoland.bedrock.tech"
    image_base: str = "https://images-fio.videoland.bedrock.tech"
    site_base: str = "https://v2.videoland.com"

    @property
    def jwt_path(self) -> str:
        return f"/v2/platforms/{self.platform}/getJwt"

    @property
    def layout_root(self) -> str:
        """``.../front/v1/<customer>/<platform>/<section>/<capacity token>``, without a trailing slash."""
        return (f"{self.layout_base}/front/v1/{self.customer}/{self.platform}/{self.section}/"
                f"{self.capacity_token}")


CLIENT_CONFIG = ClientConfig(
    salt="Ocohp1AhK0leek6aew2",
    sugar="aeSiedee3ies2Aipes5Aa",
    image_salt="nrVX5U7ws9KdNHpbnBV1kQe6nK",
    capacity_token="token-web-31",
    customer="rtlnl",
    platform="m6group_web",
    section="main",
    client_release="6.51.1",
)


# -- request signature ----------------------------------------------------------------------------
def sorted_query(params: Mapping[str, object]) -> str:
    return "&".join(f"{key}={params[key]}" for key in sorted(params))


def sign_request(path: str, query_params: Optional[Mapping[str, object]] = None, *, now: Optional[float] = None,
                 config: ClientConfig = CLIENT_CONFIG) -> dict[str, str]:
    """The ``X-Auth-Token`` / ``X-Auth-Token-Timestamp`` headers for one signed request.

    Args:
        path: The request path, e.g. ``/v2/platforms/m6group_web/getJwt``.
        query_params: Query parameters; they are signed in alphabetical key order, unencoded.
        now: Unix time in seconds (default: the current time).
        config: The client constants holding the signing key.

    Returns:
        Both headers; the token is a lowercase hex HMAC-SHA1 digest. It changes once a minute.
    """
    ts_seconds = math.floor(time.time() if now is None else now)
    message = f"{path}{sorted_query(query_params or {})}{math.floor(ts_seconds / 60)}"
    key = (config.salt + config.sugar).encode("utf-8")
    token = hmac.new(key, message.encode("utf-8"), hashlib.sha1).hexdigest()
    return {"X-Auth-Token": token, "X-Auth-Token-Timestamp": str(ts_seconds)}


# -- client ----------------------------------------------------------------------------------------
#: A fresh token is requested this many seconds after the last one (tokens last 1800 s).
JWT_LIFETIME = 25 * 60
MAX_RETRY_AFTER = 30.0
MAX_CONCURRENT_REQUESTS = 2
#: Statuses Videoland uses for a rejected/expired token.
UNAUTHORIZED_STATUSES = frozenset({401, 498})
_GATE = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)


class VideolandError(Exception):
    """Transient or unexpected failure; the message is safe to show.

    ``status`` is the HTTP status of a final non-2xx answer, else ``None``.
    """

    def __init__(self, message: str = "", status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class VideolandNotFound(VideolandError):
    """Videoland says the requested programme or page does not exist."""


class VideolandFormatError(VideolandError):
    """Videoland answered, but not in a format this plugin understands (or pagination looped)."""


class VideolandAuthError(VideolandError):
    """No usable anonymous token: nothing can be fetched for the rest of the session."""


class VideolandClientConfigError(VideolandAuthError):
    """The token service refused the request built from the known client constants.

    This means Videoland changed its web client (signing keys, client release, platform...); the
    plugin's :data:`CLIENT_CONFIG` needs an update. It is never a "not found".
    """


class _Unauthorized(Exception):
    """Internal: HTTP 401/498 on an authenticated request."""

    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


class VideolandClient:
    session: requests.Session
    device_id: str
    config: ClientConfig
    timeout: tuple[float, float]
    retries: int
    backoff: float
    sleep: Callable[[float], None]
    clock: Callable[[], float]
    request_count: int

    def __init__(
        self,
        device_id: str,
        session: Optional[requests.Session] = None,
        config: ClientConfig = CLIENT_CONFIG,
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
        retries: int = 3,
        backoff: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.session = session or requests.Session()
        self.device_id = device_id
        self.config = config
        self.timeout = (connect_timeout, read_timeout)
        self.retries = retries
        self.backoff = backoff
        self.sleep = sleep
        self.clock = clock
        self.request_count = 0
        self._jwt: Optional[str] = None
        self._jwt_at = 0.0
        self._auth_failure: Optional[VideolandAuthError] = None

    def close(self) -> None:
        self.session.close()

    # -- transport ---------------------------------------------------------------------------------
    def _send(self, url: str, context: str, headers: Optional[dict[str, str]] = None,
              params: Optional[dict[str, Any]] = None, unauthorized: bool = False) -> requests.Response:
        """Send one GET with bounded retries and return the 2xx response.

        Contract:
            Preconditions:
                - ``context`` is a short, secret-free label: it appears in every error message.
            Raises:
                _Unauthorized: HTTP 401/498 when ``unauthorized`` is true (never retried).
                VideolandNotFound: HTTP 404 (never retried).
                VideolandError: any other non-2xx status outside 429/5xx (not retried); and, once
                    the retries are exhausted, 429, 5xx, timeouts and connection errors.
        """
        all_headers = {"User-Agent": user_agent(), **(headers or {})}

        def send() -> requests.Response:
            self.request_count += 1
            with _GATE:
                return self.session.get(url, headers=all_headers, params=params, timeout=self.timeout)

        def final(response: requests.Response) -> requests.Response:
            status = response.status_code
            if 200 <= status < 300:
                return response
            if unauthorized and status in UNAUTHORIZED_STATUSES:
                raise _Unauthorized(status)
            if status == 404:
                raise VideolandNotFound(f"Not found at Videoland ({context})", status)
            raise VideolandError(f"Videoland {context} request failed (HTTP {status})", status)

        return request_with_retries(
            send, retries=self.retries, backoff=self.backoff, sleep=self.sleep, on_response=final,
            transport_problem=lambda exc: f"Videoland {context} request timed out" if isinstance(exc, requests.Timeout)
            else "Videoland unreachable",
            status_problem=lambda status: "Videoland rate limit reached" if status == 429
            else f"Videoland server error (HTTP {status}, {context})",
            give_up=lambda problem, attempts: VideolandError(problem),
            max_retry_after=MAX_RETRY_AFTER, logger=log, label=f"Videoland {context}")

    @staticmethod
    def _json_object(response: requests.Response, context: str) -> dict[str, Any]:
        """The response body as a JSON object.

        Contract:
            Raises:
                VideolandFormatError: the body is not valid JSON, or not a JSON object.
        """
        try:
            data = json.loads(response.content or b"")
        except ValueError:
            raise VideolandFormatError(f"Videoland returned a malformed response for {context}") from None
        if not isinstance(data, dict):
            raise VideolandFormatError(f"Videoland returned an unexpected response for {context}")
        return data

    # -- anonymous token ---------------------------------------------------------------------------
    def jwt(self) -> str:
        """The cached anonymous JWT, requested again when older than :data:`JWT_LIFETIME`.

        Contract:
            Raises:
                VideolandClientConfigError: the token service refused the signed request (any
                    4xx but 429), or answered without a token. Remembered for this client.
                VideolandAuthError: the token service could not be reached (after the retries)
                    or kept failing (5xx/429). Remembered for this client too: every later call
                    raises it again without a request.
        """
        if self._auth_failure is not None:
            raise self._auth_failure
        now = self.clock()
        if self._jwt is not None and now - self._jwt_at < JWT_LIFETIME:
            return self._jwt
        self._jwt = None
        path = self.config.jwt_path
        signature = sign_request(path, now=now, config=self.config)
        register_secret(signature["X-Auth-Token"])
        headers = {
            "Accept": "application/json",
            "X-Customer-Name": self.config.customer,
            "X-Client-Release": self.config.client_release,
            "x-auth-device-id": self.device_id,
            **signature,
        }
        try:
            response = self._send(self.config.auth_base + path, "anonymous token", headers=headers, unauthorized=True)
            data = self._json_object(response, "anonymous token")
        except _Unauthorized as exc:
            log.warning("Videoland token service refused the signed request (HTTP %d)", exc.status)
            raise self._fail(VideolandClientConfigError(
                f"Videoland refused the anonymous token request (HTTP {exc.status}); the plugin's Videoland "
                "client settings are probably outdated")) from None
        except VideolandFormatError:
            raise self._fail(VideolandClientConfigError(
                "Videoland's token service answered in an unexpected format; the plugin's Videoland client "
                "settings are probably outdated")) from None
        except VideolandError as exc:
            if exc.status is not None and 400 <= exc.status < 500:  # 404 included: the endpoint moved
                raise self._fail(VideolandClientConfigError(
                    f"Videoland refused the anonymous token request (HTTP {exc.status}); the plugin's Videoland "
                    "client settings are probably outdated")) from None
            raise self._fail(VideolandAuthError(f"No Videoland token: {exc}")) from None
        token = data.get("token")
        if not isinstance(token, str) or token.count(".") != 2 or not token.strip() or len(token) > 8192:
            raise self._fail(VideolandClientConfigError(
                "Videoland's token service answered without a usable token; the plugin's Videoland client "
                "settings are probably outdated"))
        register_secret(token)
        self._jwt, self._jwt_at = token, now
        return token

    def _fail(self, error: VideolandAuthError) -> VideolandAuthError:
        self._auth_failure = error
        return error

    def _authorized_json(self, url: str, context: str, params: dict[str, Any]) -> dict[str, Any]:
        """GET ``url`` with the bearer token; on 401/498 renew the token and retry exactly once.

        Contract:
            Raises:
                VideolandAuthError: no token, or the renewed token was rejected too (remembered:
                    later calls raise it without a request).
                VideolandNotFound, VideolandFormatError, VideolandError: from the request/body.
        """
        for attempt in (1, 2):
            token = self.jwt()
            try:
                response = self._send(url, context, headers={"Accept": "application/json",
                                                             "Authorization": f"Bearer {token}"},
                                      params=params, unauthorized=True)
            except _Unauthorized as exc:
                self._jwt = None
                if attempt == 1:
                    log.info("Videoland %s: token rejected (HTTP %d), requesting a new one", context, exc.status)
                    continue
                log.warning("Videoland %s: a new token was rejected too (HTTP %d)", context, exc.status)
                raise self._fail(VideolandAuthError(
                    f"Videoland rejected a new anonymous token (HTTP {exc.status}, {context}); the plugin's "
                    "Videoland client settings are probably outdated")) from None
            return self._json_object(response, context)
        raise AssertionError("unreachable")  # pragma: no cover

    # -- endpoints ---------------------------------------------------------------------------------
    def layout(self, program_id: str, block_page: int = 1) -> dict[str, Any]:
        """One page of a programme's layout (``nbPages=2``, as the web client asks).

        ``program_id`` is URL-quoted as one path segment, so it cannot change the route.
        """
        url = f"{self.config.layout_root}/program/{quote(str(program_id), safe='')}/layout"
        return self._authorized_json(url, "programme layout", {"blockPage": int(block_page), "nbPages": 2})

    def block(self, program_id: str, block_id: str, page: int) -> dict[str, Any]:
        """One further page of a layout block, addressed by the block's full ``id``."""
        url = (f"{self.config.layout_root}/program/{quote(str(program_id), safe='')}/block/"
               f"{quote(str(block_id), safe='')}")
        return self._authorized_json(url, "season episodes", {"nbPages": 3, "page": int(page)})

    def catalog_page(self, letter: str, page: int = 1) -> str:
        """The public catalog page ``/alle-content/<letter>[/<page>]`` as text (redirects followed).

        Contract:
            Raises:
                VideolandNotFound, VideolandError: from :meth:`_send`.
            Silences:
                - The body is decoded as UTF-8; invalid bytes are replaced.
        """
        path = f"/alle-content/{quote(letter, safe='')}" + (f"/{int(page)}" if page > 1 else "")
        response = self._send(self.config.site_base + path, "catalog page", headers={"Accept": "text/html"})
        return (response.content or b"").decode("utf-8", errors="replace")
