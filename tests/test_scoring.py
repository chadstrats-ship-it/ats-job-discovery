"""The scorer and its YAML configuration."""
from datetime import datetime, timezone

import pytest
import yaml

from ats_discovery import evaluate, load_config, rank, score_job
from ats_discovery.scoring import ConfigError, config_from_dict, experience_points, role_points
from conftest import mkjob

NOW = datetime(2026, 10, 7, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def cfg():
    return load_config()  # the packaged scoring.example.yaml


def test_packaged_example_loads_and_is_generic(cfg):
    assert cfg.target_roles == ["software engineer", "backend engineer", "data engineer"]
    assert cfg.skills["python"] == 1.5 and cfg.filters.require_remote is True and cfg.filters.max_age_days == 30
    assert cfg.salary.floor_usd_per_year == 100000


def test_strong_match_beats_weak_match(cfg):
    strong = mkjob("Senior Backend Engineer", "Build services in Python, SQL and AWS. 4+ years of experience.",
                   posted="2026-10-05T00:00:00+00:00", comp_min=150000, comp_max=180000, comp_unit="year")
    weak = mkjob("Customer Support Specialist", "Answer tickets.", posted="2026-10-05T00:00:00+00:00")
    s, bd = score_job(strong, cfg, now=NOW)
    w, wbd = score_job(weak, cfg, now=NOW)
    assert s >= 75 and w < 40, (s, w, bd, wbd)
    assert bd["role"]["pts"] == cfg.role_max_points  # title contains every word of "backend engineer"
    assert "python" in bd["skills"]["why"] and bd["salary"]["pts"] == cfg.salary.max_points


def test_role_points_exact_partial_and_none(cfg):
    assert role_points("Software Engineer, Payments", cfg)[0] == 40
    assert 0 < role_points("Platform Engineer", cfg)[0] < 40  # shares only the word 'engineer'
    assert role_points("Office Manager", cfg)[0] == 0


def test_skills_whole_word_matching_and_regex_keys():
    c = config_from_dict({"target_roles": ["engineer"], "skills": {"c++": 1, "go": 1, "re:\\bnode(\\.js)?\\b": 1}, "skill_saturation": 3,
                          "skill_max_points": 30})
    pts = lambda text: score_job(mkjob("Engineer", text), c, now=NOW)[1]["skills"]["pts"]  # noqa: E731
    assert pts("We use C++ and Node.js and go.") == 30
    assert pts("We love mongo and cargo.") == 0  # 'go' must not match inside other words
    assert pts("We use Node.") == 10


def test_custom_yaml_changes_ranking(cfg):
    jobs = [
        mkjob("Backend Engineer", "Python and SQL. 3+ years.", url="https://example.test/backend"),
        mkjob("Machine Learning Researcher", "PyTorch, CUDA, transformers. 3+ years.", url="https://example.test/ml"),
    ]
    default = [s.job.url for s in rank(jobs, cfg, now=NOW)]
    assert default[0].endswith("/backend")
    custom = config_from_dict(yaml.safe_load("""
target_roles: [machine learning researcher]
skills: {pytorch: 3, cuda: 3, transformers: 3}
skill_saturation: 9
filters: {title_include: []}
"""))
    flipped = [s.job.url for s in rank(jobs, custom, now=NOW)]
    assert flipped[0].endswith("/ml") and flipped != default


def test_experience_penalty_uses_yaml_range():
    c = config_from_dict({"experience": {"min_years": 2, "max_years": 5, "over_penalty_per_year": 4,
                                         "under_penalty_per_year": 3, "max_penalty": 10}})
    assert experience_points(None, c)[2] == 0 and experience_points(4, c)[2] == 0
    assert experience_points(7, c)[2] == -8 and experience_points(20, c)[2] == -10  # capped by max_penalty
    assert experience_points(0, c)[2] == -6
    assert experience_points(7, c)[0] == c.experience.base_points - 8


def test_title_boosts_and_penalties(cfg):
    base = score_job(mkjob("Software Engineer"), cfg, now=NOW)[0]
    founding = score_job(mkjob("Founding Software Engineer"), cfg, now=NOW)[0]
    staff = score_job(mkjob("Staff Software Engineer"), cfg, now=NOW)[0]
    assert founding == base + 3 and staff == base - 15


def test_remote_bonus_by_scope_and_recency(cfg):
    us = score_job(mkjob(remote_scope="us"), cfg, now=NOW)[1]["remote"]["pts"]
    worldwide = score_job(mkjob(remote_scope="worldwide"), cfg, now=NOW)[1]["remote"]["pts"]
    other = score_job(mkjob(remote_scope="other"), cfg, now=NOW)[1]["remote"]["pts"]
    assert (us, worldwide, other) == (10, 9, 0)
    fresh = score_job(mkjob(posted="2026-10-06T00:00:00+00:00"), cfg, now=NOW)[1]["recency"]["pts"]
    mid = score_job(mkjob(posted="2026-09-25T00:00:00+00:00"), cfg, now=NOW)[1]["recency"]["pts"]
    unknown = score_job(mkjob(posted=None), cfg, now=NOW)[1]["recency"]["pts"]
    assert (fresh, mid, unknown) == (6, 4, 3)


def test_salary_floor_levels_and_off_switch(cfg):
    pts = lambda **kw: score_job(mkjob(**kw), cfg, now=NOW)[1]["salary"]["pts"]  # noqa: E731
    assert pts(comp_min=120000, comp_max=150000, comp_unit="year") == 7
    assert round(pts(comp_min=80000, comp_max=130000, comp_unit="year"), 1) == 5.0
    assert pts(comp_min=40000, comp_max=60000, comp_unit="year") == 0
    assert pts() == 4  # pay not listed: neutral
    off = config_from_dict({"salary": {"floor_usd_per_year": None, "floor_usd_per_hour": None}})
    assert score_job(mkjob(comp_min=1, comp_max=2, comp_unit="year"), off, now=NOW)[1]["salary"]["pts"] == 0


def test_off_target_cap_limits_skill_stuffed_off_role_titles(cfg):
    stuffed = mkjob("Customer Advocate", "python sql aws typescript javascript react postgres docker kubernetes.",
                    posted="2026-10-06T00:00:00+00:00", comp_min=200000, comp_max=250000, comp_unit="year")
    score, bd = score_job(stuffed, cfg, now=NOW)
    assert score <= cfg.off_target.max_total
    assert "cap" in bd or bd["total_raw"] <= cfg.off_target.max_total


def test_soft_clearance_penalty_and_filtered_jobs_score_zero(cfg):
    soft = mkjob(desc="Ability to obtain a security clearance.")
    clean = mkjob(desc="Nothing special.")
    assert score_job(clean, cfg, now=NOW)[0] - score_job(soft, cfg, now=NOW)[0] == 3
    dropped = evaluate(mkjob(desc="Requires an active TS/SCI clearance."), cfg, now=NOW)
    assert dropped.score == 0 and dropped.skip_reason == "active_clearance" and dropped.breakdown["would_score"] > 0


def test_rank_orders_by_score_and_drops_filtered(cfg):
    jobs = [
        mkjob("Data Engineer", "Python SQL", url="https://example.test/b"),
        mkjob("Software Engineer", "Python SQL AWS TypeScript React", url="https://example.test/a"),
        mkjob("Sales Engineer", "Python", url="https://example.test/x"),  # title_exclude
    ]
    ranked = rank(jobs, cfg, now=NOW)
    assert [s.job.url[-1] for s in ranked] == ["a", "b"]
    assert [s.score for s in ranked] == sorted((s.score for s in ranked), reverse=True)


def test_config_rejects_typos_and_bad_types():
    with pytest.raises(ConfigError, match="unknown scoring key"):
        config_from_dict({"target_role": ["x"]})
    with pytest.raises(ConfigError, match="unknown key"):
        config_from_dict({"filters": {"require_remotee": True}})
    with pytest.raises(ConfigError):
        config_from_dict({"skills": ["python"]})
    with pytest.raises(ConfigError):
        config_from_dict(["not", "a", "mapping"])


def test_load_config_from_file(tmp_path):
    p = tmp_path / "mine.yaml"
    p.write_text("target_roles: [site reliability engineer]\nrole_max_points: 50\n", encoding="utf-8")
    c = load_config(p)
    assert c.target_roles == ["site reliability engineer"] and c.role_max_points == 50
    assert c.remote_bonus["us"] == 10  # unspecified keys keep their defaults
