"""Shared test helpers. Every test is offline: the network layer is replaced by saved fixtures or small fakes."""
import json
from pathlib import Path

import pytest

from ats_discovery import Job

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(ats: str):
    return json.loads((FIXTURES / f"{ats}.json").read_text(encoding="utf-8"))


class FakeHttp:
    """Map URL prefix -> payload (or callable(url, params)) and record every call."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple[str, dict | None]] = []

    def get_json(self, url, params=None):
        self.calls.append((url, params))
        for prefix in sorted(self.routes, key=len, reverse=True):
            if url.startswith(prefix):
                value = self.routes[prefix]
                return value(url, params) if callable(value) else value
        raise AssertionError(f"unexpected URL {url}")


def mkjob(title="Software Engineer", desc="", **kw) -> Job:
    base = dict(
        company="Acme", title=title, url="https://example.test/jobs/1", ats="greenhouse", location="Remote - US",
        remote=True, description=desc, posted=None, remote_scope="us",
    )
    base.update(kw)
    return Job(**base)


@pytest.fixture
def fixture_path():
    return lambda ats: str(FIXTURES / f"{ats}.json")
