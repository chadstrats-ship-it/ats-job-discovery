"""The normalised `Job` record every fetcher produces."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .locations import infer_scope
from .textutil import norm_employment, parse_comp

ATS_NAMES = ("greenhouse", "lever", "ashby", "workable", "recruitee", "smartrecruiters")


@dataclass
class Job:
    """One posting, normalised across ATS vendors.

    remote:       True / False when the ATS states it, None when unknown.
    remote_scope: us | worldwide | other | unknown (see locations.infer_scope).
    posted:       UTC ISO-8601 string, or None when the ATS exposes no date.
    comp_*:       USD pay parsed from structured fields or description text; comp_unit is 'year' or 'hour'.
    """

    company: str
    title: str
    url: str
    ats: str
    location: str = ""
    remote: bool | None = None
    description: str = ""
    posted: str | None = None
    apply_url: str | None = None
    employment_type: str = "unknown"
    remote_scope: str = "unknown"
    comp_min: float | None = None
    comp_max: float | None = None
    comp_unit: str | None = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        names = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in d.items() if k in names})


def make_job(
    *,
    company: str,
    title: str,
    url: str,
    ats: str,
    location: str | None = None,
    remote: bool | None = None,
    description: str | None = None,
    posted: str | None = None,
    apply_url: str | None = None,
    employment_type: str | None = None,
    comp: tuple | None = None,
) -> Job:
    """Build a Job: collapse whitespace, parse pay from the description when the ATS has none, infer remote scope."""
    company = re.sub(r"\s+", " ", (company or "").strip())
    title = re.sub(r"\s+", " ", (title or "").strip())
    location = (location or "").strip()
    description = description or ""
    lo, hi, unit = comp if comp and comp[0] is not None else parse_comp(description)
    return Job(
        company=company,
        title=title,
        url=url,
        ats=ats,
        location=location,
        remote=remote,
        description=description,
        posted=posted,
        apply_url=apply_url or url,
        employment_type=norm_employment(employment_type),
        remote_scope=infer_scope(location, title, description),
        comp_min=lo,
        comp_max=hi,
        comp_unit=unit,
    )
