"""Injectable HTTP layer.

Fetchers only need an object with `get_json(url, params=None) -> Any`. `UrllibHttp` is the real implementation
(stdlib urllib, identifying User-Agent, retry with backoff, minimum spacing between requests). `FixtureHttp` serves a
saved payload so everything runs offline; tests use small fakes of their own.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Protocol

DEFAULT_USER_AGENT = "ats-job-discovery/0.1.0 (open-source job board reader; reads public JSON endpoints only)"


class FetchError(RuntimeError):
    """A board could not be fetched or parsed."""


class NotFound(FetchError):
    """HTTP 404: the board slug does not exist on that ATS."""


class HttpClient(Protocol):
    def get_json(self, url: str, params: dict | None = None) -> Any: ...


class UrllibHttp:
    """GET JSON over HTTPS with retries on 429/5xx and a minimum interval between requests (be polite)."""

    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 40.0,
        retries: int = 2,
        min_interval: float = 0.5,
        opener: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.retries = retries
        self.min_interval = min_interval
        self._open = opener or urllib.request.urlopen
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last = 0.0

    def _throttle(self) -> None:
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
            self._last = time.monotonic()

    def get_json(self, url: str, params: dict | None = None) -> Any:
        if params:
            url = f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent, "Accept": "application/json"})
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            self._throttle()
            try:
                with self._open(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise NotFound(f"404 Not Found: {url}") from e
                last = e
                if e.code not in (429, 500, 502, 503, 504):
                    break
            except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as e:
                last = e
            if attempt < self.retries:
                self._sleep(2.0 * (attempt + 1))
        raise FetchError(f"GET {url} failed: {last}")


class FixtureHttp:
    """Serve a saved API payload instead of touching the network.

    A fixture is the JSON the list endpoint returns. SmartRecruiters needs a per-posting detail call as well, so its
    fixture may carry an extra top-level "details" object mapping posting id -> detail payload."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.requests: list[str] = []

    def get_json(self, url: str, params: dict | None = None) -> Any:
        self.requests.append(url)
        if isinstance(self.payload, dict) and "details" in self.payload and "/postings/" in url:
            pid = url.rstrip("/").rsplit("/", 1)[-1]
            try:
                return self.payload["details"][pid]
            except KeyError as e:
                raise NotFound(f"fixture has no detail for posting {pid}") from e
        return self.payload

    @classmethod
    def from_file(cls, path: str) -> "FixtureHttp":
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f))
