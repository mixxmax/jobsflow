"""JobsDB policy values must reach the real PortalCircuitBreaker."""

from __future__ import annotations

from tools.fresh_24h.portal_jd_browser import PortalCircuitBreaker
from tools.workflow.portal_policy import (
    PolicyOverrideError,
    apply_portal_overrides,
    jobsdb_runtime_config,
    resolve_workspace_profile,
)


def test_private_profile_opens_after_one_challenge(tmp_path):
    workspace = tmp_path / "JobSearch_2026"
    workspace.mkdir()
    profile = resolve_workspace_profile(workspace)
    config = jobsdb_runtime_config(profile)
    assert profile == "private"
    assert config["challenge_threshold"] == 1
    breaker = PortalCircuitBreaker(
        portal="jobsdb",
        challenge_threshold=config["challenge_threshold"],
        state_path=workspace / "circuit.json",
    )
    breaker.record_challenge("https://hk.jobsdb.com/job/1")
    assert breaker.state == "open"
    assert breaker.allow_fetch("https://hk.jobsdb.com/job/2") is False


def test_product_profile_opens_after_two_challenges(tmp_path):
    workspace = tmp_path / "product-root"
    workspace.mkdir()
    config = jobsdb_runtime_config(resolve_workspace_profile(workspace))
    assert config["challenge_threshold"] == 2
    breaker = PortalCircuitBreaker(
        portal="jobsdb",
        challenge_threshold=config["challenge_threshold"],
        state_path=workspace / "circuit.json",
    )
    breaker.record_challenge("https://hk.jobsdb.com/job/1")
    assert breaker.allow_fetch("https://hk.jobsdb.com/job/2") is True
    breaker.record_challenge("https://hk.jobsdb.com/job/2")
    assert breaker.state == "open"


def test_ordinary_payload_cannot_set_threshold_99():
    try:
        apply_portal_overrides(jobsdb_runtime_config("product"), {"challenge_threshold": 99}, diagnostic=False)
    except PolicyOverrideError as exc:
        assert "PORTAL-JDB-002" in str(exc)
    else:
        raise AssertionError("override must be refused")
