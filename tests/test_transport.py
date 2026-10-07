"""The injectable HTTP layer: retries, 404 handling, politeness, fixture serving."""
import io
import json
import urllib.error

import pytest

import ats_discovery.transport as transport
from ats_discovery import FetchError, FixtureHttp, NotFound, UrllibHttp


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def http_error(code):
    return urllib.error.HTTPError("https://x.test", code, "err", {}, io.BytesIO(b""))


def make(script, **kw):
    seen = []

    def opener(req, timeout):
        seen.append(req)
        step = script.pop(0)
        if isinstance(step, Exception):
            raise step
        return Resp(json.dumps(step).encode())

    sleeps = []
    return UrllibHttp(opener=opener, sleep=sleeps.append, min_interval=0.0, **kw), seen, sleeps


def test_get_json_sends_user_agent_and_params():
    http, seen, _ = make([{"ok": 1}])
    assert http.get_json("https://x.test/api", {"a": "1", "b": "two words"}) == {"ok": 1}
    assert seen[0].full_url == "https://x.test/api?a=1&b=two+words"
    assert seen[0].get_header("User-agent").startswith("ats-job-discovery/")


def test_retries_on_503_then_succeeds_with_backoff():
    http, seen, sleeps = make([http_error(503), http_error(429), {"ok": True}])
    assert http.get_json("https://x.test/api") == {"ok": True}
    assert len(seen) == 3 and sleeps == [2.0, 4.0]


def test_gives_up_after_retries():
    http, seen, _ = make([http_error(500)] * 3)
    with pytest.raises(FetchError):
        http.get_json("https://x.test/api")
    assert len(seen) == 3


def test_404_is_not_retried_and_is_notfound():
    http, seen, _ = make([http_error(404), {"never": "used"}])
    with pytest.raises(NotFound):
        http.get_json("https://x.test/missing")
    assert len(seen) == 1


def test_min_interval_throttles_between_requests(monkeypatch):
    sleeps = []
    clock = iter([100.0, 100.0, 100.1, 100.1])  # successive time.monotonic() readings
    monkeypatch.setattr(transport.time, "monotonic", lambda: next(clock))
    http = UrllibHttp(opener=lambda req, timeout: Resp(b"{}"), sleep=sleeps.append, min_interval=0.5)
    http.get_json("https://x.test/a")
    http.get_json("https://x.test/b")
    assert len(sleeps) == 1 and 0.39 < sleeps[0] < 0.41  # the second request waited out the remaining 0.4 s


def test_fixture_http_serves_payload_and_details():
    fx = FixtureHttp({"content": [], "details": {"7": {"id": "7"}}})
    assert fx.get_json("https://api.smartrecruiters.com/v1/companies/c/postings")["content"] == []
    assert fx.get_json("https://api.smartrecruiters.com/v1/companies/c/postings/7") == {"id": "7"}
    with pytest.raises(NotFound):
        fx.get_json("https://api.smartrecruiters.com/v1/companies/c/postings/8")
