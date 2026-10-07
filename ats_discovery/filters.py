"""Rule-based filters, ported from a private pipeline and made generic and configurable.

`evaluate_filters(job, cfg)` returns `(reason | None, flags)`. A non-None reason means "drop this posting":

    title_excluded / title_not_included   title gate (your own include / exclude regexes)
    onsite | hybrid                       not remote   (cfg.require_remote)
    remote_not_us                         remote but scoped to another region   (cfg.require_us)
    work_auth_non_us                      description demands work authorisation in a non-US country   (cfg.require_us)
    active_clearance                      an ACTIVE security clearance is mandatory   (cfg.reject_active_clearance)
    degree_required                       a degree is stated as strictly required   (cfg.reject_degree_required)
    too_many_years                        description requires more years than cfg.max_years_required
    stale                                 older than cfg.max_age_days

`flags` carries what the rules learned (remote_state, clearance, degree, years_required, amb_remote, amb_degree);
the scorer reuses it so the text is analysed once.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .locations import location_flags
from .models import Job


@dataclass
class FilterConfig:
    require_remote: bool = True
    require_us: bool = True
    reject_degree_required: bool = True
    reject_active_clearance: bool = True
    max_age_days: float | None = 30
    max_years_required: int | None = None
    title_include: list[str] = field(default_factory=list)  # regexes; empty = every title passes
    title_exclude: list[str] = field(default_factory=list)  # regexes; any match drops the job


# ------------------------------------------------------------------ title gate

def title_passes(title: str, cfg: FilterConfig) -> str | None:
    """None when the title passes, else the drop reason."""
    t = title or ""
    if any(re.search(rx, t, re.I) for rx in cfg.title_exclude):
        return "title_excluded"
    if cfg.title_include and not any(re.search(rx, t, re.I) for rx in cfg.title_include):
        return "title_not_included"
    return None


# ------------------------------------------------------------------ remote / on-site

_STRONG_REMOTE = re.compile(
    r"(fully[- ]remote|100% remote|remote[- ]first|remote[- ]only|this (is|will be) a remote|(is|a) remote (role|position|job|opportunity)|"
    r"work(ing)? remotely|remote within the (us|u\.s\.|united states)|remote \(?(us|usa|u\.s\.)\)?|"
    r"location:?\s*remote|(we are|we're) a (fully )?(remote|distributed)|distributed team|anywhere in the (us|united states|world)(?!.{0,40}(up to|per year|weeks|days))|"
    r"remote[- ]eligible|can be (done )?remote(ly)?|may be (done )?remote(ly)?|remote[- ]friendly|(us|u\.s\.|usa)[- ]remote|eligible for remote|"
    r"remote (work )?(is )?(available|an option|possible|allowed|permitted|supported)|work remotely|remote or (in[- ]office|hybrid|onsite))",
    re.I,
)
_WEAK_REMOTE = re.compile(r"\bremote(ly)?\b", re.I)
_ONSITE_TEXT = re.compile(
    r"(hybrid (work|workplace|schedule|model|role|position|environment|arrangement|policy|setup|working)|(this|the) (role|position) is (a )?(hybrid|on-?site|in[- ]office)|"
    r"\b\d\s*(-|to)?\s*\d?\s*days?\s*(a|per|each|/)\s*(week|wk)\s*(in|at|from)\s*(the|our|an)\s*(office|hq|headquarters|studio)|"
    r"(must|required to|will need to|expected to)\s*(be\s*)?(work|located|based|reside|live|commute|come)[^.\n]{0,30}(on-?site|in[- ]office|in the office|in[- ]person|at our (office|hq)|within \d+ miles)|"
    r"relocation (is )?(required|necessary)|must (be willing to )?relocate|on-?site (only|required|position|role)|in[- ]office (only|required))",
    re.I,
)
_REMOTE_NEGATED = re.compile(
    r"\b(not|non)[- ]remote(?![- ]?(only|first|friendly))|\bno remote\b|remote (work )?(is )?not (available|offered|possible)|(unable|cannot) (to )?(offer|support) remote",
    re.I,
)
_NON_US_AUTH = re.compile(
    r"(must|need to|have to|required to)\s+(be\s+)?(legally\s+)?(authori[sz]ed|eligible|entitled|permitted|able)\s+to\s+work\s+in\s+(the\s+)?"
    r"(uk|u\.k\.|united kingdom|eu|e\.u\.|european union|canada|india|australia|germany|ireland|netherlands|france|spain|poland|brazil|mexico)\b",
    re.I,
)


def remote_state(job: Job) -> tuple[str, bool]:
    """-> (state, ambiguous) where state is remote | onsite | hybrid | unverified."""
    flag = job.remote
    loc, title, desc = job.location or "", job.title or "", job.description or ""
    lf = location_flags(loc, title)
    if re.search(r"\bhybrid\b|\bon[- ]?site\b", title, re.I):  # explicit in the title beats a generic remote flag
        return "hybrid", False
    if flag is False:
        return ("hybrid" if lf["hybrid"] or re.search(r"hybrid", loc + title, re.I) else "onsite"), False
    if lf["hybrid"] and not lf["remote"]:
        return "hybrid", False
    if flag is True or lf["remote"]:
        return "remote", False
    if _STRONG_REMOTE.search(desc):
        return "remote", False
    if _WEAK_REMOTE.search(desc) and not _REMOTE_NEGATED.search(desc):
        return "unverified", True
    return "onsite", False


# ------------------------------------------------------------------ age

def age_days(job: Job, now: datetime | None = None) -> float | None:
    if not job.posted:
        return None
    try:
        d = datetime.fromisoformat(job.posted)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - d).total_seconds() / 86400


# ------------------------------------------------------------------ security clearance
# Hard skip ONLY when an ACTIVE/current clearance is mandatory. "Eligible to obtain", "may require", "preferred" etc.
# are soft: they return "eligible" and the scorer applies a small penalty.
_CLR_WORD = re.compile(r"(clearance|ts/?sci|top[- ]secret|polygraph)", re.I)
_CLR_SOFT = re.compile(
    r"(eligib\w*|may (be )?(require|need)\w*|might (require|need)|(able|ability|willing|capable) to (obtain|get|acquire|apply)|"
    r"obtain(ing)? (and|or) maintain|can obtain|able to be granted|prefer\w*|nice[- ]to[- ]have|a plus|bonus|desirable|ideal(ly)?|"
    r"(not|no|n't)\s+(be\s+)?(require\w*|necessary|needed|mandatory))",
    re.I,
)
_CLR_ACTIVE = re.compile(
    r"(ts/?sci|top[- ]secret|\bpolygraph\b|"
    r"\b(active|current|currently|existing|valid)\b[^.\n]{0,40}(clearance|\bsecret\b|\bts\b|\bdod\b)|"
    r"\bsecret\s+clearance|"
    r"clearance\s+(is\s+)?(required|mandatory|needed)|"
    r"must\s+(currently\s+)?(have|hold|possess|maintain)\s+(an?\s+)?[a-z/ -]{0,25}clearance)",
    re.I,
)


def clearance_status(text: str) -> str | None:
    """'active' (mandatory active/current clearance) | 'eligible' (eligibility / may require / preferred) | None.

    Judged on a window around each clearance mention (bounded by sentence ends / newlines; descriptions scraped without
    newlines run sentences together, so the trailing window is short)."""
    found = None
    for m in _CLR_WORD.finditer(text):
        lo = max(text.rfind(c, 0, m.start()) for c in ".!?;\n")
        lo = max(lo + 1, m.start() - 110)
        hi_cands = [i for i in (text.find(c, m.end()) for c in ".!?;\n") if i != -1]
        hi = min(min(hi_cands) if hi_cands else len(text), m.end() + 60)
        seg = text[lo:hi]
        if _CLR_ACTIVE.search(seg) and not _CLR_SOFT.search(seg):
            return "active"
        found = "eligible"
    return found


# ------------------------------------------------------------------ degree requirement

_DEG_TERM = re.compile(
    r"(bachelor'?s?|\bb\.?\s?s\.?c?\b|\bb\.?a\.?\b|master'?s?|\bm\.?s\.?c?\b|\bmba\b|ph\.?\s?d\.?|doctorate|doctoral|"
    r"(college|university|four[- ]year|4[- ]year)\s+degree|\bdegree\b|undergraduate|graduate degree)",
    re.I,
)
_EQUIV = re.compile(
    r"(or equivalent|equivalent\s+(\w+\s+){0,3}(experience|education|training|knowledge|combination)|"
    r"or (comparable|related|relevant|similar|practical|work|professional|industry|hands-on|demonstrated|proven|real[- ]world|equivalent)\s+(\w+\s+){0,2}(experience|background|knowledge|skills?|work|projects?|portfolio)|"
    r"in lieu of|without (a )?degree|degree (is )?(not (strictly )?(required|necessary|a requirement)|optional)|no degree (is )?(required|needed)|"
    r"not (require|requiring|requires)\s+(a\s+)?(formal\s+)?(college\s+)?degree|self[- ]taught|bootcamp|(or|and/or)\s+(a\s+)?(combination|equivalent)|"
    r"or foreign equivalent|degree\s+(or|and/or)\s+(experience|equivalent)|equivalent combination|"
    r"\bor\s+(at\s+least\s+)?\d+\+?\s*(years?|yrs?)['’]?\s*(of\s+)?(\w+\s+){0,3}experience|"
    r"(a\s+)?degree\s+(is\s+)?(preferred|a plus|desired|nice|bonus|helpful|beneficial|not mandatory)|"
    r"(preferred|desired|nice to have|bonus|a plus|ideally)[^.\n]{0,60}(degree|bachelor|master|ph\.?d)|"
    r"(degree|bachelor|master|ph\.?d)[^.\n]{0,60}(preferred|desired|a plus|nice to have|bonus|ideal))",
    re.I,
)
_PREF_HEAD = re.compile(r"(preferred|nice[- ]to[- ]have|bonus|a plus|good to have|desirable|ideal(ly)?|extra credit|even better|great to have)", re.I)
_REQ_HEAD = re.compile(
    r"(requirements?|qualifications?|what (we('| a)re|you('| wi)ll need)|must[- ]have|minimum|you have|about you|who you are|what you bring|skills)",
    re.I,
)
_REQ_WORD = re.compile(r"\b(required?|requires?|must|mandatory|minimum|necessary)\b", re.I)


def degree_status(text: str) -> str:
    """strict | equiv | optional | none | ambiguous.

    'strict' only when a degree is stated as required with no equivalent-experience clause. A plain "Qualifications"
    wish list is 'ambiguous'; only Required / Minimum / Basic / must-have headings or must/required wording is strict."""
    lines = []
    for raw in re.split(r"\n+", text):
        raw = raw.strip()
        if len(raw) > 280:  # descriptions scraped without newlines: fall back to sentence-ish splitting
            lines += [p.strip() for p in re.split(r"(?<=[.;:])\s*(?=[A-Z0-9])", raw) if p.strip()]
        elif raw:
            lines.append(raw)
    section = "req"
    strict_lines, deg_lines, strict_secs = [], [], []
    for l in lines:
        short = len(l) < 70
        if short and _PREF_HEAD.search(l) and not _DEG_TERM.search(l):
            section = "pref"
            continue
        if short and _REQ_HEAD.search(l) and not _DEG_TERM.search(l):
            section = "req_strict" if (_REQ_WORD.search(l) or re.search(r"\bbasic\b|must[- ]have", l, re.I)) else "req"
            continue
        if not _DEG_TERM.search(l):
            continue
        # a bare word 'degree' in unrelated phrases ("360 degree", "to a degree") is ignored
        if not re.search(r"(bachelor|master|ph\.?d|doctor|college|university|undergrad|\bb\.?s\.?\b|\bm\.?s\.?\b|\bb\.?a\.?\b|degree (in|from|or|and|is|required)|degree,)", l, re.I):
            continue
        if re.search(r"\b(360|ninety|180)[- ]degree|to (some|a|an?) degree|degree of (freedom|autonomy|flexib|independ|ownership)", l, re.I):
            continue
        deg_lines.append(l)
        if section == "pref" or _PREF_HEAD.search(l) or _EQUIV.search(l):
            continue
        strict_lines.append(l)
        strict_secs.append(section)
    if not deg_lines:
        return "none"
    doc_equiv = bool(_EQUIV.search(text))
    if not strict_lines:
        return "equiv" if doc_equiv else "optional"
    if doc_equiv:
        return "equiv"
    if any(_REQ_WORD.search(l) for l in strict_lines) or any(sec == "req_strict" for sec in strict_secs):
        return "strict"
    return "ambiguous"


# ------------------------------------------------------------------ years of experience

_YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:(?:-|–|to)\s*(\d{1,2})\s*\+?\s*)?(?:years?|yrs?)\b", re.I)
_PREF_SECT = re.compile(
    r"(preferred (qualifications?|skills?|experience|requirements?)|nice[- ]to[- ]haves?|bonus (points?|skills?|qualifications?)|"
    r"extra credit|good to have|great to have|even better|what would make you stand out|desirable (skills?|qualifications?|experience))",
    re.I,
)
_REQ_SECT = re.compile(
    r"((required|minimum|basic) (qualifications?|skills?|experience|requirements?)|requirements?|(what|who) you('ll| will)? (bring|need|are|have)|"
    r"must[- ]haves?|you have|about you|qualifications?|education (and|&) experience|(skills|experience) (and|&) (skills|experience|education)|"
    r"what we('re| are) looking for|your (background|experience|profile)|responsibilities|what you('ll| will) do|about (us|the (role|team|job))|benefits|compensation)",
    re.I,
)


def _in_preferred_section(text: str, pos: int) -> bool:
    """True when the nearest section header before `pos` is a preferred/nice-to-have header."""
    win = text[max(0, pos - 1500): pos]
    pref = max((m.end() for m in _PREF_SECT.finditer(win)), default=-1)
    req = max((m.end() for m in _REQ_SECT.finditer(win)), default=-1)
    return pref >= 0 and pref >= req


def years_required(text: str) -> int | None:
    """Highest hard-requirement minimum years found near 'experience' (ignores preferred / bonus context)."""
    best = None
    for m in _YEARS.finditer(text):
        ctx = text[max(0, m.start() - 90): m.end() + 70]
        if not re.search(r"experience|working|professional|industry|building|developing|engineering|hands-on|background|in (a|the)", ctx, re.I):
            continue
        pre = re.split(r"[\n.;]", text[max(0, m.start() - 70): m.start()])[-1]  # same line/sentence only
        if re.search(r"(prefer|bonus|plus|nice|ideal|desired|a plus)", pre, re.I) or _in_preferred_section(text, m.start()):
            continue
        if re.search(r"(company|we've|we have|we are|founded|over|more than|with|team of|customers|for|history)\s+(the\s+)?(last\s+)?$", pre[-25:], re.I) and not re.search(r"experience", ctx, re.I):
            continue
        y = int(m.group(1))
        if y > 25:
            continue
        best = y if best is None else max(best, y)
    return best


# ------------------------------------------------------------------ the combined gate

def evaluate_filters(job: Job, cfg: FilterConfig, now: datetime | None = None) -> tuple[str | None, dict]:
    """Apply every enabled rule. Returns (drop_reason | None, flags)."""
    text = f"{job.title}\n{job.description}"
    flags: dict = {}

    state, amb_remote = remote_state(job)
    flags["remote_state"] = state
    if amb_remote:
        flags["amb_remote"] = True
    clr = clearance_status(text)
    if clr:
        flags["clearance"] = clr
    yrs = years_required(job.description)
    flags["years_required"] = yrs
    deg = degree_status(job.description)
    flags["degree"] = deg
    if deg == "ambiguous":
        flags["amb_degree"] = True

    reason = title_passes(job.title, cfg)
    if reason:
        return reason, flags
    if cfg.require_remote:
        if state in ("onsite", "hybrid"):
            return state, flags
        # a remote flag from the ATS overrides on-site-sounding boilerplate; otherwise obey the text
        if job.remote is not True and _ONSITE_TEXT.search(job.description) and state != "remote":
            return "onsite", flags
        if _REMOTE_NEGATED.search(job.description) and job.remote is not True:
            return "onsite", flags
    if cfg.require_us:
        if job.remote_scope == "other" and state in ("remote", "unverified"):
            return "remote_not_us", flags
        if _NON_US_AUTH.search(job.description) and job.remote_scope != "us":
            return "work_auth_non_us", flags
    if cfg.reject_active_clearance and clr == "active":
        return "active_clearance", flags
    if cfg.max_years_required is not None and yrs is not None and yrs > cfg.max_years_required:
        return "too_many_years", flags
    if cfg.reject_degree_required and deg == "strict":
        return "degree_required", flags
    if cfg.max_age_days is not None:
        a = age_days(job, now)
        if a is not None and a > cfg.max_age_days:
            return "stale", flags
    return None, flags
