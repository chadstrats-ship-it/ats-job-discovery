"""One test group per filter rule (remote, US, degree, clearance, years, age, title)."""
from datetime import datetime, timezone

from ats_discovery import FilterConfig, evaluate_filters
from ats_discovery.filters import (
    age_days, clearance_status, degree_status, remote_state, title_passes, years_required,
)
from ats_discovery.locations import infer_scope
from ats_discovery.textutil import html_to_text, parse_comp
from conftest import mkjob

NOW = datetime(2026, 10, 7, tzinfo=timezone.utc)


def reason(job, **cfg):
    return evaluate_filters(job, FilterConfig(**cfg), NOW)[0]


# ---- remote ------------------------------------------------------------------------------------------------------

def test_remote_state_precedence():
    assert remote_state(mkjob(remote=True, location="Remote - US"))[0] == "remote"
    assert remote_state(mkjob(remote=False, location="New York, NY"))[0] == "onsite"
    assert remote_state(mkjob(remote=False, location="Hybrid - Austin, TX"))[0] == "hybrid"
    assert remote_state(mkjob(title="Engineer (Hybrid)", remote=True))[0] == "hybrid"  # title beats a generic flag
    assert remote_state(mkjob(remote=None, location="", desc="This is a fully remote role."))[0] == "remote"
    assert remote_state(mkjob(remote=None, location="", desc="Some work may happen remotely."))[0] == "unverified"
    assert remote_state(mkjob(remote=None, location="", desc="Come to our office."))[0] == "onsite"


def test_require_remote_drops_onsite_and_hybrid():
    assert reason(mkjob(remote=False, location="New York, NY")) == "onsite"
    assert reason(mkjob(remote=False, location="Hybrid - Boston")) == "hybrid"
    assert reason(mkjob(remote=None, location="", desc="You will be in office 3 days a week in the office.", remote_scope="unknown")) == "onsite"
    assert reason(mkjob(remote=None, location="", desc="This role is not remote.", remote_scope="unknown")) == "onsite"
    assert reason(mkjob(remote=False, location="New York, NY"), require_remote=False) is None
    assert reason(mkjob(remote=True, desc="Remote-first, but not remote-only company.")) is None  # negation regex must not misfire


# ---- US ----------------------------------------------------------------------------------------------------------

def test_scope_inference():
    assert infer_scope("Remote - US") == "us" and infer_scope("Remote - Dallas") == "us"
    assert infer_scope("Remote (EMEA)") == "other" and infer_scope("Anywhere in the World") == "worldwide"
    assert infer_scope("Bulgaria", "Automation Specialist, Global Services", "Fully distributed team") == "other"
    assert infer_scope("Bulgaria", "Engineer", "We are open to applicants from anywhere in the world.") == "worldwide"
    assert infer_scope("Remote", "Engineer", "Open to candidates residing in the US.") == "us"
    assert infer_scope("Remote", "Engineer", "Solutions for companies worldwide.") == "unknown"


def test_require_us_drops_non_us_remote_and_foreign_work_auth():
    assert reason(mkjob(location="Remote (EMEA)", remote_scope="other")) == "remote_not_us"
    assert reason(mkjob(location="Remote (EMEA)", remote_scope="other"), require_us=False) is None
    assert reason(mkjob(location="Remote", remote_scope="unknown", desc="You must be authorized to work in the UK.")) == "work_auth_non_us"
    assert reason(mkjob(location="Remote - US", remote_scope="us", desc="Must be authorized to work in the UK.")) is None
    assert reason(mkjob(remote=None, location="Berlin", remote_scope="other", desc="Parts can be done remotely.")) == "remote_not_us"


# ---- degree ------------------------------------------------------------------------------------------------------

def test_degree_status_classes():
    assert degree_status("Requirements\n- Bachelor's degree in Computer Science or equivalent practical experience") == "equiv"
    assert degree_status("Requirements\n- Bachelor's degree in Computer Science or related field") == "ambiguous"
    assert degree_status("Requirements\n- 3 years Python\nNice to have\n- Bachelor's degree in CS") in ("optional", "none")
    assert degree_status("A Bachelor's degree is preferred but not required") != "strict"
    assert degree_status("We love a 360-degree view of the customer") == "none"
    assert degree_status("Required Qualifications Bachelor's degree in computer science.8+ years of software engineering experience.") == "strict"
    assert degree_status("Minimum qualifications:\n- Bachelor's degree in Engineering.") == "strict"


def test_reject_degree_required_toggle():
    job = mkjob(desc="Required qualifications:\nBachelor's degree is required.\n")
    assert reason(job) == "degree_required"
    assert reason(job, reject_degree_required=False) is None
    assert reason(mkjob(desc="Bachelor's degree or equivalent experience.")) is None


# ---- clearance ---------------------------------------------------------------------------------------------------

def test_clearance_status_active_vs_eligible():
    assert clearance_status("This position requires an active Secret clearance.") == "active"
    assert clearance_status("Must hold an active TS/SCI.") == "active"
    assert clearance_status("Candidate must have an active TS/SCI clearance with Polygraph.About the Team:") == "active"
    assert clearance_status("Must be eligible for a U.S. security clearance; a background check is required.") == "eligible"
    assert clearance_status("This position may require eligibility to obtain and maintain a security clearance.") == "eligible"
    assert clearance_status("Active TS/SCI preferred") == "eligible"
    assert clearance_status("Great benefits and a friendly team.") is None


def test_reject_active_clearance_toggle_and_soft_flag():
    hard = mkjob(desc="You must hold an active Top Secret clearance.")
    assert reason(hard) == "active_clearance"
    assert reason(hard, reject_active_clearance=False) is None
    soft_reason, flags = evaluate_filters(mkjob(desc="Ability to obtain a security clearance."), FilterConfig(), NOW)
    assert soft_reason is None and flags["clearance"] == "eligible"


# ---- years, age, title -------------------------------------------------------------------------------------------

def test_years_required_ignores_preferred_text():
    assert years_required("Requirements\n- 3 years Python experience\nNice to have\n- 10+ years of experience") == 3
    assert years_required("Computer Science or a related field preferred\n5-7 years of professional experience as a full-stack engineer") == 5
    assert years_required("8+ years of experience programming with Python") == 8
    assert years_required("We have been around for 12 years and love it.") is None


def test_max_years_required_rule():
    job = mkjob(desc="You bring 9+ years of professional experience.")
    assert reason(job) is None
    assert reason(job, max_years_required=6) == "too_many_years"


def test_stale_rule_and_age_days():
    old = mkjob(posted="2026-06-01T00:00:00+00:00")
    assert round(age_days(old, NOW)) == 128
    assert reason(old) == "stale"
    assert reason(old, max_age_days=None) is None
    assert reason(mkjob(posted="2026-10-01T00:00:00+00:00")) is None
    assert age_days(mkjob(posted=None), NOW) is None


def test_title_include_exclude():
    cfg = FilterConfig(title_include=["engineer|developer"], title_exclude=["sales"])
    assert title_passes("Backend Engineer", cfg) is None
    assert title_passes("Sales Engineer", cfg) == "title_excluded"
    assert title_passes("Office Manager", cfg) == "title_not_included"
    assert title_passes("Anything at all", FilterConfig()) is None


# ---- text helpers used by the rules ------------------------------------------------------------------------------

def test_html_to_text_handles_double_escaped_html_and_blocks():
    text = html_to_text("&lt;p&gt;One&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Two&lt;/li&gt;&lt;/ul&gt;")
    assert [line for line in text.split("\n") if line] == ["One", "Two"]
    assert html_to_text("<style>p{}</style><p>Hi&nbsp;there</p>") == "Hi there"


def test_parse_comp():
    assert parse_comp("$55 - $70 per hour") == (55.0, 70.0, "hour")
    assert parse_comp("Base salary $150,000 - $180,000 per year") == (150000.0, 180000.0, "year")
    assert parse_comp("$120k-$150k") == (120000.0, 150000.0, "year")
    assert parse_comp("no pay listed") == (None, None, None)
