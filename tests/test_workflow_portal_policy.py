"""P3: JobsDB runtime policy is configured, not chosen ad-hoc by the model."""

from __future__ import annotations

import pytest

from tools.workflow.portal_policy import (
    apply_portal_overrides,
    jobsdb_runtime_config,
    PolicyOverrideError,
)


def test_product_and_private_thresholds_are_declared():
    product = jobsdb_runtime_config("product")
    private = jobsdb_runtime_config("private")
    assert product["challenge_threshold"] == 2
    assert private["challenge_threshold"] == 1
    assert product["max_challenge_retries"] == 0
    assert private["max_challenge_retries"] == 0
    assert product["cache_first"] is True
    assert product["allow_model_override"] is False
    assert product["human_verification_handoff"] is False
    assert private["human_verification_handoff"] is True
    assert private["verification_timeout_seconds"] == 600


def test_model_cannot_override_circuit_without_diagnostic_flag():
    with pytest.raises(PolicyOverrideError, match="PORTAL-JDB-002"):
        apply_portal_overrides(
            jobsdb_runtime_config("product"),
            {"challenge_threshold": 99, "max_challenge_retries": 3},
            diagnostic=False,
        )


def test_diagnostic_override_is_explicit_limited_and_auditable():
    result = apply_portal_overrides(
        jobsdb_runtime_config("product"),
        {"challenge_threshold": 1},
        diagnostic=True,
        actor="agent",
    )
    assert result["config"]["challenge_threshold"] == 1
    assert result["config"]["max_challenge_retries"] == 0
    assert result["diagnostic"] is True
    assert "PORTAL-JDB-002" in result["rule_ids"]
    assert result["audit"]["override_keys"] == ["challenge_threshold"]
    assert "max_challenge_retries" not in result["audit"]["override_keys"]


def test_unknown_override_keys_are_rejected_even_when_diagnostic():
    with pytest.raises(PolicyOverrideError, match="unknown"):
        apply_portal_overrides(
            jobsdb_runtime_config("product"),
            {"proxy_pool": True},
            diagnostic=True,
        )


def test_private_profile_is_consumed_by_real_two_pass_breaker(tmp_path, monkeypatch):
    private = tmp_path / "JobSearch_2026"
    (private / "00_Profile").mkdir(parents=True)
    (private / "00_Profile" / "queries.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("JOBSEARCH_ROOT", str(private))
    monkeypatch.delenv("PORTAL_JD_MIN_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("PORTAL_JD_MAX_REQUESTS_PER_SCAN", raising=False)

    from tools.fresh_24h.two_pass_score import run_two_pass

    _rows, meta = run_two_pass([], repo=tmp_path, max_deep=0, sleep_s=0)
    assert meta["jobsdb_policy"]["profile"] == "private"
    assert meta["jobsdb_policy"]["challenge_threshold"] == 1
    assert meta["jobsdb_policy"]["human_verification_handoff"] is True
    from tools.fresh_24h import portal_jd_browser

    assert portal_jd_browser._PORTAL_BUDGET_STATE["jobsdb"]["min_interval"] == 15
    assert portal_jd_browser._PORTAL_BUDGET_STATE["jobsdb"]["max_per_scan"] == 10
