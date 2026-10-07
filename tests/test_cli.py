"""CLI behaviour, offline: --fixture replaces the network; batch mode is exercised with a patched fetcher."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import ats_discovery.cli as cli
from ats_discovery import ATS_NAMES, FixtureHttp, fetch_jobs
from ats_discovery.cli import main
from conftest import load_fixture

AS_OF = "2026-10-07"
ROOT = Path(__file__).resolve().parents[1]


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_table_output_sorted_by_score(capsys, fixture_path):
    code, out, err = run(capsys, "--company", "stripe", "--ats", "greenhouse", "--fixture", fixture_path("greenhouse"),
                         "--no-filters", "--as-of", AS_OF)
    assert code == 0
    lines = out.strip().splitlines()
    assert lines[0].split() == ["SCORE", "COMPANY", "TITLE", "LOCATION", "URL"]
    scores = [int(l.split()[0]) for l in lines[2:]]
    assert len(scores) == 3 and scores == sorted(scores, reverse=True)
    assert "Stripe" in out and "https://stripe.com/jobs/search?gh_jid=" in out
    assert "3 fetched" in err


@pytest.mark.parametrize("ats", ATS_NAMES)
def test_json_output_for_every_ats(capsys, fixture_path, ats):
    code, out, _ = run(capsys, "--company", "demo", "--ats", ats, "--fixture", fixture_path(ats), "--json", "--no-filters", "--as-of", AS_OF)
    assert code == 0
    rows = json.loads(out)
    assert len(rows) == 3
    for r in rows:
        assert {"score", "company", "title", "location", "url", "ats", "remote", "breakdown"} <= set(r)
        assert r["ats"] == ats and 0 <= r["score"] <= 100 and "description" not in r
    assert [r["score"] for r in rows] == sorted((r["score"] for r in rows), reverse=True)


def test_filters_drop_jobs_and_limit_and_min_score(capsys, fixture_path):
    base = ("--company", "stripe", "--ats", "greenhouse", "--fixture", fixture_path("greenhouse"), "--json", "--as-of", AS_OF)
    _, out, err = run(capsys, *base)
    kept = json.loads(out)
    assert "2 filtered out" in err  # the 2024 posting is stale; the Chicago one is not remote
    assert [k["title"] for k in kept] == ["Abuse Research Engineer"]
    _, out, _ = run(capsys, *base, "--no-filters", "--limit", "1")
    assert len(json.loads(out)) == 1
    _, out, _ = run(capsys, *base, "--no-filters", "--min-score", "99")
    assert json.loads(out) == []


def test_custom_config_changes_output_order(capsys, fixture_path, tmp_path):
    cfgfile = tmp_path / "scoring.yaml"
    cfgfile.write_text(yaml.safe_dump({"target_roles": ["ai engineer"], "skills": {}, "title_boosts": {"abuse": 30}}), encoding="utf-8")
    argv = ["--company", "stripe", "--ats", "greenhouse", "--fixture", fixture_path("greenhouse"), "--json", "--no-filters", "--as-of", AS_OF]
    _, default_out, _ = run(capsys, *argv)
    _, custom_out, _ = run(capsys, *argv, "--config", str(cfgfile))
    assert json.loads(default_out)[0]["title"] != json.loads(custom_out)[0]["title"]
    assert json.loads(custom_out)[0]["title"] == "Abuse Research Engineer"


def test_argument_errors(capsys, fixture_path):
    assert run(capsys)[0] == 2
    assert run(capsys, "--company", "x")[0] == 2  # --ats missing
    assert run(capsys, "--all-companies", "c.yaml", "--company", "x", "--ats", "lever")[0] == 2
    assert run(capsys, "--company", "x", "--ats", "lever", "--fixture", "nope.json")[0] == 2  # unreadable fixture
    bad = run(capsys, "--company", "x", "--ats", "lever", "--fixture", fixture_path("lever"), "--config", "does-not-exist.yaml")
    assert bad[0] == 2 and "error" in bad[2]
    with pytest.raises(SystemExit):
        main(["--company", "x", "--ats", "workday"])  # not an allowed --ats choice


def test_wrong_fixture_for_ats_reports_failure(capsys, fixture_path):
    code, _, err = run(capsys, "--company", "x", "--ats", "lever", "--fixture", fixture_path("greenhouse"))
    assert code == 2 and "unexpected payload" in err


def test_print_config_outputs_packaged_example(capsys):
    code, out, _ = run(capsys, "--print-config")
    assert code == 0 and "target_roles:" in out and yaml.safe_load(out)["filters"]["require_us"] is True


def test_all_companies_batch_mode(capsys, monkeypatch, tmp_path):
    listing = tmp_path / "companies.yaml"
    listing.write_text(yaml.safe_dump({"companies": [
        {"ats": "greenhouse", "slug": "stripe", "name": "Stripe"},
        {"ats": "lever", "slug": "spotify", "name": "Spotify"},
        {"ats": "ashby", "slug": "gone", "name": "Gone"},
    ]}), encoding="utf-8")

    def fake_fetch(ats, slug, http, company=None):
        if slug == "gone":
            from ats_discovery import NotFound
            raise NotFound("404")
        return fetch_jobs(ats, slug, FixtureHttp(load_fixture(ats)), company=company)

    monkeypatch.setattr(cli, "fetch_jobs", fake_fetch)
    code, out, err = run(capsys, "--all-companies", str(listing), "--json", "--no-filters", "--as-of", AS_OF)
    assert code == 0
    rows = json.loads(out)
    assert {r["company"] for r in rows} == {"Stripe", "Spotify"} and len(rows) == 6
    assert "warning: ashby/gone" in err
    code, out, _ = run(capsys, "--all-companies", str(listing), "--ats", "lever", "--json", "--no-filters", "--as-of", AS_OF)
    assert {r["company"] for r in json.loads(out)} == {"Spotify"}


def test_example_companies_file_is_valid():
    rows = cli._load_companies(str(ROOT / "companies.example.yaml"))
    assert 20 <= len(rows) <= 30
    assert {r["ats"] for r in rows} == set(ATS_NAMES)
    assert len({(r["ats"], r["slug"]) for r in rows}) == len(rows)


def test_module_entry_point_via_subprocess(fixture_path):
    proc = subprocess.run(
        [sys.executable, "-m", "ats_discovery", "--company", "spotify", "--ats", "lever", "--fixture", fixture_path("lever"),
         "--no-filters", "--limit", "2", "--as-of", AS_OF],
        capture_output=True, text=True, encoding="utf-8", cwd=ROOT, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    rows = proc.stdout.strip().splitlines()
    assert rows[0].startswith("SCORE") and len(rows) == 4  # header, rule, 2 jobs
