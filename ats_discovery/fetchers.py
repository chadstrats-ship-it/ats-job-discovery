"""One fetcher per ATS. Each takes a board slug and an injectable HTTP client and returns normalised `Job`s.

Endpoints (all public and unauthenticated):

  greenhouse       GET boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
  lever            GET api.lever.co/v0/postings/{slug}?mode=json
  ashby            GET api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true
  workable         GET apply.workable.com/api/v1/widget/accounts/{slug}?details=true
  recruitee        GET {slug}.recruitee.com/api/offers/
  smartrecruiters  GET api.smartrecruiters.com/v1/companies/{slug}/postings (list), then /postings/{id} (detail)
"""
from __future__ import annotations

from typing import Callable

from .models import ATS_NAMES, Job, make_job
from .textutil import html_to_text, iso
from .transport import FetchError, HttpClient, UrllibHttp

GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
LEVER_URL = "https://api.lever.co/v0/postings/{slug}"
ASHBY_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}"
WORKABLE_URL = "https://apply.workable.com/api/v1/widget/accounts/{slug}"
RECRUITEE_URL = "https://{slug}.recruitee.com/api/offers/"
SMARTRECRUITERS_URL = "https://api.smartrecruiters.com/v1/companies/{slug}/postings"
SMARTRECRUITERS_PUBLIC = "https://jobs.smartrecruiters.com/{company}/{id}"


def _workplace_flag(value: str | None) -> bool | None:
    """Lever / Ashby workplaceType -> remote flag. Only an explicit 'remote' is True; hybrid/onsite are False."""
    v = (value or "").lower()
    if v == "remote":
        return True
    if v in ("hybrid", "onsite", "on-site"):
        return False
    return None


def fetch_greenhouse(slug: str, http: HttpClient, company: str | None = None) -> list[Job]:
    data = http.get_json(GREENHOUSE_URL.format(slug=slug), params={"content": "true"})
    if not isinstance(data, dict) or "jobs" not in data:
        raise FetchError(f"greenhouse/{slug}: unexpected payload")
    out = []
    for j in data["jobs"]:
        flag = None
        for m in j.get("metadata") or []:
            if (m.get("name") or "").lower() == "location type" and m.get("value"):
                v = str(m["value"]).lower()
                flag = True if v.startswith("remote") else (False if v.startswith(("on-site", "onsite", "hybrid")) else None)
        out.append(make_job(
            company=company or j.get("company_name") or slug,
            title=j["title"],
            url=j["absolute_url"],
            ats="greenhouse",
            location=(j.get("location") or {}).get("name") or "",
            posted=iso(j.get("first_published") or j.get("updated_at")),
            description=html_to_text(j.get("content")),
            remote=flag,
        ))
    return out


def fetch_lever(slug: str, http: HttpClient, company: str | None = None) -> list[Job]:
    data = http.get_json(LEVER_URL.format(slug=slug), params={"mode": "json"})
    if not isinstance(data, list):
        raise FetchError(f"lever/{slug}: unexpected payload")
    out = []
    for j in data:
        cat = j.get("categories") or {}
        locs = cat.get("allLocations") or ([cat["location"]] if cat.get("location") else [])
        parts = [j.get("descriptionPlain") or html_to_text(j.get("description"))]
        for lst in j.get("lists") or []:
            parts.append(f"{lst.get('text', '')}\n{html_to_text(lst.get('content'))}")
        parts.append(j.get("additionalPlain") or "")
        comp = None
        sr = j.get("salaryRange")
        if isinstance(sr, dict) and sr.get("min") and sr.get("currency", "USD") == "USD":
            hourly = "hour" in str(sr.get("interval", "")).lower()
            comp = (sr["min"], sr.get("max") or sr["min"], "hour" if hourly else "year")
        out.append(make_job(
            company=company or slug,
            title=j["text"],
            url=j["hostedUrl"],
            apply_url=j.get("applyUrl"),
            ats="lever",
            location=" | ".join(locs),
            employment_type=cat.get("commitment"),
            posted=iso(j.get("createdAt")),
            description="\n\n".join(p for p in parts if p),
            comp=comp,
            remote=_workplace_flag(j.get("workplaceType")),
        ))
    return out


def fetch_ashby(slug: str, http: HttpClient, company: str | None = None) -> list[Job]:
    data = http.get_json(ASHBY_URL.format(slug=slug), params={"includeCompensation": "true"})
    if not isinstance(data, dict) or "jobs" not in data:
        raise FetchError(f"ashby/{slug}: unexpected payload")
    out = []
    for j in data["jobs"]:
        if j.get("isListed") is False:
            continue
        comp = None
        for c in (j.get("compensation") or {}).get("summaryComponents") or []:
            if c.get("compensationType") == "Salary" and c.get("currencyCode") == "USD" and c.get("minValue"):
                unit = "hour" if "HOUR" in str(c.get("interval", "")).upper() else "year"
                comp = (c["minValue"], c.get("maxValue") or c["minValue"], unit)
                break
        locs = [j.get("location") or ""] + [
            s.get("location", "") if isinstance(s, dict) else str(s) for s in j.get("secondaryLocations") or []
        ]
        out.append(make_job(
            company=company or slug,
            title=j["title"],
            url=j["jobUrl"],
            apply_url=j.get("applyUrl"),
            ats="ashby",
            location=" | ".join(x for x in locs if x),
            employment_type=j.get("employmentType"),
            posted=iso(j.get("publishedAt")),
            description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
            comp=comp,
            # `isRemote` is true for Hybrid postings in the real payload, so only workplaceType is trusted.
            remote=_workplace_flag(j.get("workplaceType")),
        ))
    return out


def fetch_workable(slug: str, http: HttpClient, company: str | None = None) -> list[Job]:
    data = http.get_json(WORKABLE_URL.format(slug=slug), params={"details": "true"})
    if not isinstance(data, dict) or "jobs" not in data:
        raise FetchError(f"workable/{slug}: unexpected payload")
    out = []
    for j in data["jobs"]:
        loc = ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x)
        remote = True if j.get("telecommuting") else None
        out.append(make_job(
            company=company or data.get("name") or slug,
            title=j["title"],
            url=j.get("shortlink") or j["url"],
            apply_url=j.get("application_url"),
            ats="workable",
            location=("Remote - " + loc) if remote else loc,
            employment_type=j.get("employment_type"),
            posted=iso(j.get("published_on") or j.get("created_at")),
            description=html_to_text(j.get("description")),
            remote=remote,
        ))
    return out


def _recruitee_location(j: dict) -> str:
    """'Remote job' alone hides the country; append the posting's countries so scope inference sees them."""
    loc = j.get("location") or ""
    countries = sorted(
        {l.get("country") for l in j.get("locations") or [] if l.get("country")} | ({j["country"]} if j.get("country") else set())
    )
    return f"{loc} ({', '.join(countries)})" if countries and not any(c in loc for c in countries) else loc


def fetch_recruitee(slug: str, http: HttpClient, company: str | None = None) -> list[Job]:
    data = http.get_json(RECRUITEE_URL.format(slug=slug))
    if not isinstance(data, dict) or "offers" not in data:
        raise FetchError(f"recruitee/{slug}: unexpected payload")
    out = []
    for j in data["offers"]:
        if j.get("status") not in (None, "published"):
            continue
        flag = True if j.get("remote") else (False if (j.get("hybrid") or j.get("on_site")) else None)
        sal = j.get("salary") or {}
        comp = None
        if sal.get("min") and (sal.get("currency") or "USD").upper() == "USD":
            hourly = "hour" in str(sal.get("period")).lower()
            comp = (sal["min"], sal.get("max") or sal["min"], "hour" if hourly else "year")
        desc = html_to_text(j.get("description")) + "\n\n" + html_to_text(j.get("requirements"))
        out.append(make_job(
            company=company or j.get("company_name") or slug,
            title=j["title"],
            url=j["careers_url"],
            apply_url=j.get("careers_apply_url"),
            ats="recruitee",
            location=_recruitee_location(j),
            employment_type=j.get("employment_type_code"),
            posted=iso(j.get("published_at")),
            description=desc.strip(),
            comp=comp,
            remote=flag,
        ))
    return out


def fetch_smartrecruiters(
    slug: str,
    http: HttpClient,
    company: str | None = None,
    max_postings: int = 500,
    max_details: int = 30,
    detail_filter: Callable[[dict], bool] | None = None,
) -> list[Job]:
    """List postings (100 per page, up to `max_postings`), then fetch the detail payload (the only place the job
    description lives) for at most `max_details` postings that pass `detail_filter`. The rest are still returned,
    with an empty description, so a title-only score is possible without hammering the API."""
    url = SMARTRECRUITERS_URL.format(slug=slug)
    postings: list[dict] = []
    offset = 0
    while offset < max_postings:
        data = http.get_json(url, params={"limit": 100, "offset": offset})
        if not isinstance(data, dict) or "content" not in data:
            raise FetchError(f"smartrecruiters/{slug}: unexpected payload")
        content = data["content"] or []
        postings += content
        offset += 100
        if not content or offset >= (data.get("totalFound") or 0):
            break
    out, details_used = [], 0
    for p in postings:
        loc = p.get("location") or {}
        remote = True if loc.get("remote") else (False if loc.get("hybrid") else None)
        full = loc.get("fullLocation") or loc.get("country") or ""
        location = f"Remote - {full}" if remote else full
        ident = (p.get("company") or {}).get("identifier") or slug
        posting_url = SMARTRECRUITERS_PUBLIC.format(company=ident, id=p["id"])
        apply_url = None
        desc = ""
        if details_used < max_details and (detail_filter is None or detail_filter(p)):
            try:
                d = http.get_json(f"{url}/{p['id']}")
            except FetchError:
                d = None
            details_used += 1
            if isinstance(d, dict):
                secs = (d.get("jobAd") or {}).get("sections") or {}
                desc = "\n\n".join(
                    html_to_text((secs.get(k) or {}).get("text"))
                    for k in ("jobDescription", "qualifications", "additionalInformation")
                    if secs.get(k)
                )
                posting_url = d.get("postingUrl") or d.get("applyUrl") or posting_url
                apply_url = d.get("applyUrl")
        out.append(make_job(
            company=company or (p.get("company") or {}).get("name") or slug,
            title=p["name"],
            url=posting_url,
            apply_url=apply_url,
            ats="smartrecruiters",
            location=location,
            employment_type=(p.get("typeOfEmployment") or {}).get("label"),
            posted=iso(p.get("releasedDate")),
            description=desc,
            remote=remote,
        ))
    return out


FETCHERS: dict[str, Callable[..., list[Job]]] = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workable": fetch_workable,
    "recruitee": fetch_recruitee,
    "smartrecruiters": fetch_smartrecruiters,
}
assert tuple(FETCHERS) == ATS_NAMES


def fetch_jobs(ats: str, slug: str, http: HttpClient | None = None, company: str | None = None, **kw) -> list[Job]:
    """Fetch every posting on one company board. `http` defaults to a polite `UrllibHttp`."""
    try:
        fetcher = FETCHERS[ats]
    except KeyError:
        raise ValueError(f"unknown ats {ats!r}; choose one of {', '.join(ATS_NAMES)}") from None
    return fetcher(slug, http or UrllibHttp(), company=company, **kw)
