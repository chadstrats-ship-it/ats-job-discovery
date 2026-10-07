"""Rule-based 0-100 scorer driven entirely by a small YAML file (see scoring.example.yaml and the README).

Components (each reported in `ScoredJob.breakdown`):

    role        how closely the title matches any of `target_roles`            0 .. role_max_points
    skills      weighted keyword hits in the title + description                0 .. skill_max_points
    remote      bonus by remote scope (us / worldwide / unknown / ...)
    salary      pay vs. your floor (optional; neutral when pay is not listed)
    recency     how fresh the posting is
    experience  base points minus a penalty when the posting asks for more (or fewer) years than your range
    title       sum of title_boosts minus title_penalties that match
    clearance   small penalty when a clearance is only "eligible / may require"

An off-target cap keeps jobs whose title barely resembles a target role from ranking high on skills alone.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import yaml

from . import filters
from .filters import FilterConfig
from .models import Job

DEFAULT_CONFIG_PATH = Path(__file__).with_name("scoring.example.yaml")


class ConfigError(ValueError):
    """The scoring YAML has an unknown key or a wrong type."""


@dataclass
class ExperienceConfig:
    base_points: float = 10.0
    min_years: float = 0.0
    max_years: float = 6.0
    over_penalty_per_year: float = 5.0
    under_penalty_per_year: float = 0.0
    max_penalty: float = 30.0


@dataclass
class SalaryConfig:
    floor_usd_per_year: float | None = None
    floor_usd_per_hour: float | None = None
    max_points: float = 7.0
    unknown_points: float = 4.0


@dataclass
class OffTargetConfig:
    role_fraction: float = 0.4
    max_total: float = 54.0


@dataclass
class ScoringConfig:
    target_roles: list[str] = field(default_factory=list)
    role_max_points: float = 40.0
    skills: dict[str, float] = field(default_factory=dict)
    skill_max_points: float = 25.0
    skill_saturation: float = 14.0
    remote_bonus: dict[str, float] = field(
        default_factory=lambda: {"us": 10.0, "worldwide": 9.0, "unknown": 4.5, "other": 0.0, "unverified": 3.0}
    )
    salary: SalaryConfig = field(default_factory=SalaryConfig)
    recency: dict[float, float] = field(default_factory=lambda: {3: 6.0, 7: 5.0, 14: 4.0, 21: 2.5, 30: 1.5})
    recency_unknown_points: float = 3.0
    experience: ExperienceConfig = field(default_factory=ExperienceConfig)
    title_boosts: dict[str, float] = field(default_factory=dict)
    title_penalties: dict[str, float] = field(default_factory=dict)
    clearance_soft_penalty: float = 3.0
    off_target: OffTargetConfig = field(default_factory=OffTargetConfig)
    description_chars: int = 3500
    filters: FilterConfig = field(default_factory=FilterConfig)


_SECTIONS = {"salary": SalaryConfig, "experience": ExperienceConfig, "off_target": OffTargetConfig, "filters": FilterConfig}


def config_from_dict(raw: dict[str, Any] | None) -> ScoringConfig:
    """Validate a parsed YAML mapping and build a ScoringConfig. Unknown keys raise ConfigError (typos fail loudly)."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("scoring config must be a mapping")
    raw = dict(raw)
    known = {f.name for f in fields(ScoringConfig)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ConfigError(f"unknown scoring key(s): {', '.join(unknown)}; valid keys: {', '.join(sorted(known))}")
    kwargs: dict[str, Any] = {}
    for key, value in raw.items():
        if key in _SECTIONS:
            cls = _SECTIONS[key]
            if value is None:
                value = {}
            if not isinstance(value, dict):
                raise ConfigError(f"'{key}' must be a mapping")
            bad = sorted(set(value) - {f.name for f in fields(cls)})
            if bad:
                raise ConfigError(f"unknown key(s) in '{key}': {', '.join(bad)}")
            kwargs[key] = cls(**value)
        elif key == "recency":
            if not isinstance(value, dict):
                raise ConfigError("'recency' must map max-age-days to points")
            kwargs[key] = {float(k): float(v) for k, v in value.items()}
        elif key in ("skills", "title_boosts", "title_penalties", "remote_bonus"):
            if not isinstance(value, dict):
                raise ConfigError(f"'{key}' must be a mapping")
            kwargs[key] = {str(k): float(v) for k, v in value.items()}
        elif key == "target_roles":
            if not isinstance(value, list):
                raise ConfigError("'target_roles' must be a list of strings")
            kwargs[key] = [str(v) for v in value]
        else:
            kwargs[key] = value
    cfg = ScoringConfig(**kwargs)
    merged = {"us": 10.0, "worldwide": 9.0, "unknown": 4.5, "other": 0.0, "unverified": 3.0}
    merged.update(cfg.remote_bonus)
    cfg.remote_bonus = merged
    return cfg


def load_config(path: str | Path | None = None) -> ScoringConfig:
    """Load a scoring YAML; with no path, the packaged scoring.example.yaml."""
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(p, encoding="utf-8") as f:
        return config_from_dict(yaml.safe_load(f))


# ------------------------------------------------------------------ scoring

@dataclass
class ScoredJob:
    job: Job
    score: int
    breakdown: dict
    skip_reason: str | None = None
    flags: dict = field(default_factory=dict)

    def to_dict(self, include_description: bool = False) -> dict:
        d = self.job.to_dict()
        if not include_description:
            d.pop("description", None)
        d.pop("extra", None)
        d.update(score=self.score, skip_reason=self.skip_reason, breakdown=self.breakdown, flags=self.flags)
        return d


_LEVEL_WORDS = re.compile(
    r"\b(senior|sr|junior|jr|staff|principal|lead|mid|entry|level|i{1,3}|iv|remote|us|usa|contract|contractor|freelance)\b", re.I
)


def _clean_title(t: str) -> str:
    t = re.sub(r"\(.*?\)|\[.*?\]|[-–—|,:/].*$", " ", t.lower())
    return re.sub(r"\s+", " ", _LEVEL_WORDS.sub(" ", t)).strip()


def _tokens(t: str) -> set[str]:
    return set(re.findall(r"[a-z0-9+#.]+", t.lower()))


def _token_sort_ratio(a: str, b: str) -> float:
    ta, tb = " ".join(sorted(a.split())), " ".join(sorted(b.split()))
    return SequenceMatcher(None, ta, tb).ratio() * 100


def role_points(title: str, cfg: ScoringConfig) -> tuple[float, str]:
    """Best match over target_roles. All role words in the title = full points; otherwise a fuzzy / partial score."""
    if not cfg.target_roles:
        return 0.0, "no target_roles configured"
    cleaned = _clean_title(title)
    title_tokens = _tokens(title)
    best_frac, best_why = 0.0, "no target role matches"
    for role in cfg.target_roles:
        role_tokens = _tokens(role)
        if not role_tokens:
            continue
        coverage = len(role_tokens & title_tokens) / len(role_tokens)
        if coverage == 1.0:
            frac, why = 1.0, f"title contains '{role}'"
        else:
            fuzzy = max(0.0, min(1.0, (_token_sort_ratio(cleaned, role.lower()) - 60) / 40)) * 0.9
            partial = 0.5 * coverage if coverage >= 0.5 else 0.0
            frac = max(fuzzy, partial)
            why = f"partial match to '{role}'"
        if frac > best_frac:
            best_frac, best_why = frac, why
    return cfg.role_max_points * best_frac, best_why


def _skill_regex(term: str) -> re.Pattern:
    if term.startswith("re:"):
        return re.compile(term[3:], re.I)
    return re.compile(r"(?<![A-Za-z0-9_+#])" + re.escape(term) + r"(?![A-Za-z0-9_+#])", re.I)


def skill_points(job: Job, cfg: ScoringConfig) -> tuple[float, str]:
    text = f"{job.title}\n{(job.description or '')[: cfg.description_chars]}"
    total, hits = 0.0, []
    for term, weight in cfg.skills.items():
        if _skill_regex(term).search(text):
            total += weight
            hits.append(term)
    pts = cfg.skill_max_points * min(1.0, total / cfg.skill_saturation) if cfg.skill_saturation > 0 else 0.0
    return pts, ", ".join(hits[:12]) or "no skill keywords found"


def remote_points(scope: str, state: str, cfg: ScoringConfig) -> tuple[float, str]:
    if state == "unverified":
        return cfg.remote_bonus.get("unverified", 0.0), "remote unverified"
    if state != "remote":
        return cfg.remote_bonus.get(state, 0.0), state
    return cfg.remote_bonus.get(scope, cfg.remote_bonus.get("unknown", 0.0)), f"remote-{scope}"


def salary_points(job: Job, cfg: ScoringConfig) -> tuple[float, str]:
    s = cfg.salary
    if s.floor_usd_per_year is None and s.floor_usd_per_hour is None:
        return 0.0, "salary scoring off"
    lo, hi, unit = job.comp_min, job.comp_max, job.comp_unit
    if not lo or not unit:
        return s.unknown_points, "pay not listed (neutral)"
    hi = hi or lo
    if unit == "hour":
        floor = s.floor_usd_per_hour
    elif job.employment_type == "contract" and s.floor_usd_per_hour is not None:
        lo, hi, floor = lo / 2080, hi / 2080, s.floor_usd_per_hour
    else:
        floor = s.floor_usd_per_year
    if floor is None:
        return s.unknown_points, "no floor for this pay unit (neutral)"
    if lo >= floor:
        return s.max_points, f"pay min {lo:g} >= floor {floor:g}"
    if hi >= floor:
        return s.max_points * 5 / 7, f"pay range {lo:g}-{hi:g} reaches floor {floor:g}"
    return 0.0, f"pay max {hi:g} below floor {floor:g}"


def recency_points(job: Job, cfg: ScoringConfig, now: datetime | None) -> tuple[float, str]:
    a = filters.age_days(job, now)
    if a is None:
        return cfg.recency_unknown_points, "date unknown"
    for limit in sorted(cfg.recency):
        if a <= limit:
            return cfg.recency[limit], f"{a:.0f}d old"
    return 0.0, f"{a:.0f}d old"


def experience_points(yrs: int | None, cfg: ScoringConfig) -> tuple[float, str, float]:
    e = cfg.experience
    pen = 0.0
    if yrs is not None:
        if yrs > e.max_years:
            pen = -e.over_penalty_per_year * (yrs - e.max_years)
        elif yrs < e.min_years:
            pen = -e.under_penalty_per_year * (e.min_years - yrs)
    pen = max(-e.max_penalty, pen)
    why = "years unspecified" if yrs is None else f"{yrs}y asked (range {e.min_years:g}-{e.max_years:g})"
    return e.base_points + pen, why, pen


def title_points(title: str, cfg: ScoringConfig) -> tuple[float, str]:
    total, notes = 0.0, []
    for rx, w in cfg.title_boosts.items():
        if re.search(rx, title, re.I):
            total += w
            notes.append(f"+{w:g} {rx}")
    for rx, w in cfg.title_penalties.items():
        if re.search(rx, title, re.I):
            total -= abs(w)
            notes.append(f"-{abs(w):g} {rx}")
    return total, ", ".join(notes) or "no title adjustments"


def score_job(job: Job, cfg: ScoringConfig, flags: dict | None = None, now: datetime | None = None) -> tuple[int, dict]:
    """Score one job 0-100. `flags` (from filters.evaluate_filters) avoids re-analysing the text."""
    flags = flags or {}
    state = flags.get("remote_state") or filters.remote_state(job)[0]
    yrs = flags["years_required"] if "years_required" in flags else filters.years_required(job.description or "")
    parts: dict[str, tuple[float, str]] = {}
    parts["role"] = role_points(job.title, cfg)
    parts["skills"] = skill_points(job, cfg)
    parts["remote"] = remote_points(job.remote_scope, state, cfg)
    parts["salary"] = salary_points(job, cfg)
    parts["recency"] = recency_points(job, cfg, now)
    exp_pts, exp_why, exp_pen = experience_points(yrs, cfg)
    parts["experience"] = (exp_pts, exp_why)
    parts["title"] = title_points(job.title, cfg)
    clr = flags["clearance"] if "clearance" in flags else filters.clearance_status(f"{job.title}\n{job.description or ''}")
    if clr == "eligible":
        parts["clearance"] = (-abs(cfg.clearance_soft_penalty), "clearance eligibility / may-require mentioned (soft)")
    total = sum(p[0] for p in parts.values())
    role_factor = parts["role"][0] / cfg.role_max_points if cfg.role_max_points else 1.0
    capped = False
    if cfg.target_roles and role_factor < cfg.off_target.role_fraction and total > cfg.off_target.max_total:
        total, capped = cfg.off_target.max_total, True
    bd: dict[str, Any] = {k: {"pts": round(v[0], 1), "why": v[1]} for k, v in parts.items()}
    bd["experience"]["penalty"] = exp_pen
    bd["experience"]["years"] = yrs
    bd["total_raw"] = round(total, 1)
    if capped:
        bd["cap"] = f"role below {cfg.off_target.role_fraction:g} of max; capped at {cfg.off_target.max_total:g}"
    return int(round(max(0.0, min(100.0, total)))), bd


def evaluate(job: Job, cfg: ScoringConfig, apply_filters: bool = True, now: datetime | None = None) -> ScoredJob:
    """Run the filters (optional) then score. A filtered job keeps its would-be score in the breakdown and scores 0."""
    if apply_filters:
        reason, flags = filters.evaluate_filters(job, cfg.filters, now)
    else:
        reason, flags = None, {}
    sc, bd = score_job(job, cfg, flags, now)
    if reason:
        bd["would_score"] = sc
        return ScoredJob(job, 0, bd, reason, flags)
    return ScoredJob(job, sc, bd, None, flags)


def evaluate_all(jobs: list[Job], cfg: ScoringConfig, apply_filters: bool = True, now: datetime | None = None) -> list[ScoredJob]:
    return [evaluate(j, cfg, apply_filters, now) for j in jobs]


def sort_scored(scored: list[ScoredJob]) -> list[ScoredJob]:
    """Drop filtered jobs and sort the rest by score (ties: newest first, then title)."""
    kept = [s for s in scored if s.skip_reason is None]
    kept.sort(key=lambda s: (-s.score, _neg_posted(s.job), s.job.title))
    return kept


def rank(jobs: list[Job], cfg: ScoringConfig, apply_filters: bool = True, now: datetime | None = None) -> list[ScoredJob]:
    """Evaluate every job and return the survivors, best first."""
    return sort_scored(evaluate_all(jobs, cfg, apply_filters, now))


def _neg_posted(job: Job) -> float:
    if not job.posted:
        return 0.0
    try:
        d = datetime.fromisoformat(job.posted)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return -d.timestamp()
    except ValueError:
        return 0.0
