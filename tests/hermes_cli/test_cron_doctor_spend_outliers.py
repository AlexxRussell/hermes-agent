"""`hermes cron doctor` flags a fire that burned far more tokens than the job usually does."""

import json

from cron import jobs
from hermes_cli import cron


def _audit(tmp_path, monkeypatch, fires):
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir()
    monkeypatch.setattr(jobs, "CRON_DIR", cron_dir)
    lines = [json.dumps({"job_id": job_id, "total_tokens": tokens}) for job_id, tokens in fires]
    (cron_dir / "usage_audit.jsonl").write_text("\n".join(lines + ["not json"]) + "\n", encoding="utf-8")


def test_last_fire_far_above_the_usual_is_reported(tmp_path, monkeypatch):
    # total_tokens=null = a run that failed before usage was known: out of the baseline, not zero.
    usual = [("runaway", 10_000_000)] * 4 + [("runaway", None)] * 6 + [("steady", 10_000_000)] * 6 + [("tiny", 50_000)] * 5
    _audit(tmp_path, monkeypatch, usual + [("runaway", 84_000_000), ("tiny", 400_000)])

    outliers = cron._cron_spend_outliers()

    assert set(outliers) == {"runaway"}  # steady is normal, tiny stays under the 1M floor
    assert "84.0M tokens, 8x its usual 10.0M" in outliers["runaway"]
    issues = cron._cron_doctor_issues_for_job({"id": "runaway", "next_run_at": None, "state": "paused"},
                                              outliers)
    assert issues == [outliers["runaway"]]


def test_a_record_cut_mid_character_is_skipped_like_any_bad_line(tmp_path, monkeypatch):
    # The scheduler writes raw UTF-8 (ensure_ascii=False); an append cut inside an error's "—"
    # leaves bytes that are not UTF-8, and the next append lands on the same line.
    _audit(tmp_path, monkeypatch, [])
    torn = b'{"job_id": "job", "total_tokens": null, "error": "provider down ' + "—".encode()[:2]
    fires = [json.dumps({"job_id": "job", "total_tokens": t}).encode() for t in [10_000_000] * 5 + [84_000_000]]
    (jobs.CRON_DIR / "usage_audit.jsonl").write_bytes(b"\n".join(fires[:3] + [torn + fires[3]] + fires[4:]) + b"\n")

    assert "84.0M tokens" in cron._cron_spend_outliers()["job"]


def test_warning_clears_once_the_next_fire_is_normal(tmp_path, monkeypatch):
    _audit(tmp_path, monkeypatch, [("job", 10_000_000)] * 5 + [("job", 84_000_000), ("job", 11_000_000)])

    assert cron._cron_spend_outliers() == {}
