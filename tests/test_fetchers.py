"""Each fetcher's parsing / normalisation, against saved real payloads (3 postings per ATS) and tiny synthetic ones."""
import pytest

from ats_discovery import ATS_NAMES, FetchError, FixtureHttp, Job, fetch_jobs
from ats_discovery.fetchers import (
    fetch_ashby, fetch_greenhouse, fetch_lever, fetch_recruitee, fetch_smartrecruiters, fetch_workable,
)
from conftest import FakeHttp, load_fixture


def fetch_fixture(ats, company=None):
    return fetch_jobs(ats, "demo", FixtureHttp(load_fixture(ats)), company=company)


@pytest.mark.parametrize("ats", ATS_NAMES)
def test_every_fixture_yields_three_normalised_jobs(ats):
    jobs = fetch_fixture(ats)
    assert len(jobs) == 3
    for j in jobs:
        assert isinstance(j, Job) and j.ats == ats
        assert j.title and j.url.startswith("https://") and j.company
        assert j.remote in (True, False, None)
        assert j.remote_scope in ("us", "worldwide", "other", "unknown")
        assert "<" not in j.description and "&lt;" not in j.description  # HTML fully converted to text
        assert j.posted is None or j.posted.endswith("+00:00")  # normalised to UTC ISO-8601


def test_greenhouse_parsing():
    j = {x.title: x for x in fetch_fixture("greenhouse")}["Abuse Research Engineer"]
    assert j.company == "Stripe" and j.location == "Remote from the US" and j.remote_scope == "us"
    assert j.url.startswith("https://stripe.com/jobs/search?gh_jid=")
    assert j.posted == "2026-09-09T14:52:09+00:00"  # first_published, converted from -04:00 to UTC
    assert "Stripe" in j.description


def test_greenhouse_company_override_and_location_type_metadata():
    payload = {"jobs": [{"title": "T", "absolute_url": "https://x.test/1", "location": {"name": "Berlin"},
                         "metadata": [{"name": "Location Type", "value": "Remote"}], "content": "&lt;p&gt;Hi&lt;/p&gt;"}]}
    (job,) = fetch_greenhouse("co", FixtureHttp(payload), company="Custom Name")
    assert job.company == "Custom Name" and job.remote is True and job.description == "Hi"


def test_lever_parsing_workplace_and_pay():
    jobs = {x.title: x for x in fetch_fixture("lever")}
    remote = jobs["Data Scientist - Music Mission"]
    assert remote.remote is True and remote.location == "New York, NY" and remote.remote_scope == "us"
    assert (remote.comp_min, remote.comp_max, remote.comp_unit) == (116994.0, 167135.0, "year")  # from salaryRange
    assert remote.employment_type == "fulltime" and remote.apply_url.endswith("/apply")
    hybrid = jobs["Backend Engineer - Data Platform"]
    assert hybrid.remote is False and hybrid.location == "Stockholm | London"  # allLocations joined


def test_ashby_parsing_ignores_isremote_and_reads_compensation():
    jobs = {x.title: x for x in fetch_fixture("ashby")}
    remote = jobs["Software Engineer, Security, Stablecoin"]
    assert remote.remote is True and (remote.comp_min, remote.comp_max, remote.comp_unit) == (189000, 330000, "year")
    assert "San Francisco" in remote.location  # secondary locations are appended
    hybrid = jobs["Software Engineer, Frontend"]
    assert hybrid.remote is False  # the raw payload says isRemote=true for this Hybrid posting; workplaceType wins


def test_ashby_skips_unlisted_postings():
    payload = {"jobs": [{"title": "Hidden", "jobUrl": "https://x.test/h", "isListed": False},
                        {"title": "Shown", "jobUrl": "https://x.test/s", "workplaceType": "Remote", "descriptionPlain": "Python"}]}
    jobs = fetch_ashby("co", FixtureHttp(payload))
    assert [j.title for j in jobs] == ["Shown"] and jobs[0].remote is True


def test_workable_parsing_prefixes_remote_locations():
    jobs = {x.title: x for x in fetch_fixture("workable")}
    us = jobs["Senior Open-Source Python Engineer, ML Developer Tools - US Remote"]
    assert us.company == "Hugging Face" and us.remote is True
    assert us.location.startswith("Remote - ") and us.remote_scope == "us"
    assert us.url.startswith("https://apply.workable.com/j/") and us.apply_url.endswith("/apply")
    assert jobs["Senior Machine Learning Engineer, Voice Agents - EMEA Remote"].remote_scope == "other"


def test_recruitee_parsing_hybrid_and_unpublished():
    jobs = fetch_fixture("recruitee")
    assert all(j.remote is False for j in jobs)  # all three are hybrid offices
    assert jobs[0].company == "bunq" and jobs[0].url.startswith("https://careers.bunq.com/o/")
    payload = {"offers": [{"title": "Draft", "careers_url": "https://x.test/d", "status": "draft"},
                          {"title": "Live", "careers_url": "https://x.test/l", "status": "published", "remote": True,
                           "description": "<p>Do things</p>", "requirements": "<ul><li>Python</li></ul>"}]}
    (live,) = fetch_recruitee("co", FixtureHttp(payload))
    assert live.remote is True and "Do things" in live.description and "Python" in live.description


def test_smartrecruiters_list_then_detail_and_remote_prefix():
    http = FixtureHttp(load_fixture("smartrecruiters"))
    jobs = {x.title: x for x in fetch_smartrecruiters("servicenow", http)}
    remote = jobs["Sr Staff Software Engineer ( AI Agents, Observability)"]
    assert remote.remote is True and remote.location.startswith("Remote - ") and remote.description
    assert remote.url.startswith("https://jobs.smartrecruiters.com/ServiceNow/")
    assert sum("/postings/" in u for u in http.requests) == 3  # one detail call per posting


def test_smartrecruiters_pagination_and_detail_cap():
    def page(url, params):
        offset = params["offset"]
        return {"totalFound": 150, "content": [
            {"id": str(offset + i), "name": f"Engineer {offset + i}", "company": {"identifier": "Co", "name": "Co"},
             "location": {"fullLocation": "Austin, Texas, United States", "remote": False}}
            for i in range(100 if offset == 0 else 50)]}

    base = "https://api.smartrecruiters.com/v1/companies/co/postings"
    http = FakeHttp({
        base + "/": {"jobAd": {"sections": {"jobDescription": {"text": "<p>Detail text</p>"}}}},
        base: page,
    })
    jobs = fetch_smartrecruiters("co", http, max_details=5)
    assert len(jobs) == 150
    assert [bool(j.description) for j in jobs[:6]] == [True] * 5 + [False]  # only 5 detail calls, the rest are title-only
    assert sum("/postings/" in u for u, _ in http.calls) == 5
    assert jobs[0].url == "https://jobs.smartrecruiters.com/Co/0"


@pytest.mark.parametrize(
    "fn", [fetch_greenhouse, fetch_lever, fetch_ashby, fetch_workable, fetch_recruitee, fetch_smartrecruiters]
)
def test_unexpected_payload_raises_fetch_error(fn):
    with pytest.raises(FetchError):
        fn("co", FixtureHttp({"unexpected": True}))


def test_unknown_ats_is_rejected():
    with pytest.raises(ValueError, match="unknown ats"):
        fetch_jobs("workday", "co", FixtureHttp({}))


def test_endpoints_and_params_requested():
    gh = FakeHttp({"https://boards-api.greenhouse.io/v1/boards/acme/jobs": {"jobs": []}})
    fetch_greenhouse("acme", gh)
    assert gh.calls == [("https://boards-api.greenhouse.io/v1/boards/acme/jobs", {"content": "true"})]
    lv = FakeHttp({"https://api.lever.co/v0/postings/acme": []})
    fetch_lever("acme", lv)
    assert lv.calls[0][1] == {"mode": "json"}
    rc = FakeHttp({"https://acme.recruitee.com/api/offers/": {"offers": []}})
    fetch_recruitee("acme", rc)
    assert rc.calls[0][0] == "https://acme.recruitee.com/api/offers/"
