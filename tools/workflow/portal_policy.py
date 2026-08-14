"""JobsDB runtime policy. Models cannot change thresholds or retry budgets."""

from __future__ import annotations

import os
import warnings
from pathlib import Path
from typing import Any

ALLOWED_DIAGNOSTIC_KEYS = {"challenge_threshold"}

PROFILES = {
    "product": {
        "challenge_threshold": 2,
        "max_challenge_retries": 0,
        "cache_first": True,
        "allow_model_override": False,
        "max_requests_per_scan": 10,
        "min_interval_seconds": 15,
    },
    "private": {
        "challenge_threshold": 1,
        "max_challenge_retries": 0,
        "cache_first": True,
        "allow_model_override": False,
        "max_requests_per_scan": 10,
        "min_interval_seconds": 15,
    },
}


class PolicyOverrideError(ValueError):
    pass


def resolve_workspace_profile(workspace: Path | str | None = None) -> str:
    if workspace is None:
        return "product"
    path = Path(workspace)
    if path.name == "JobSearch_2026":
        return "private"
    return "product"


def jobsdb_runtime_config(profile: str = "product") -> dict[str, Any]:
    if profile not in PROFILES:
        warnings.warn(
            f"unknown JobsDB profile {profile!r}; failing closed to product defaults",
            RuntimeWarning,
            stacklevel=2,
        )
        return dict(PROFILES["product"])
    return dict(PROFILES[profile])


def apply_jobsdb_config_to_runtime(config: dict[str, Any], *, portal: str = "jobsdb") -> dict[str, Any]:
    """Push policy values into the process-wide JobsDB request budget."""
    from tools.fresh_24h import portal_jd_browser as browser

    state = browser._PORTAL_BUDGET_STATE.setdefault(portal, browser._budget_defaults())
    if "PORTAL_JD_MIN_INTERVAL_SECONDS" not in os.environ:
        state["min_interval"] = float(config.get("min_interval_seconds") or 15)
    if "PORTAL_JD_MAX_REQUESTS_PER_SCAN" not in os.environ:
        state["max_per_scan"] = float(config.get("max_requests_per_scan") or 10)
    return dict(config)


def apply_portal_overrides(
    base: dict[str, Any],
    overrides: dict[str, Any] | None,
    *,
    diagnostic: bool = False,
    actor: str = "agent",
) -> dict[str, Any]:
    overrides = dict(overrides or {})
    if not overrides:
        return {"config": dict(base), "diagnostic": False, "rule_ids": ["PORTAL-JDB-002"], "audit": {}}
    if not diagnostic:
        raise PolicyOverrideError("PORTAL-JDB-002: model cannot override JobsDB runtime policy")
    unknown = sorted(key for key in overrides if key not in ALLOWED_DIAGNOSTIC_KEYS)
    if unknown:
        raise PolicyOverrideError(f"unknown portal override keys: {unknown}")
    config = dict(base)
    applied = {}
    for key, value in overrides.items():
        config[key] = value
        applied[key] = value
    config["allow_model_override"] = False
    return {
        "config": config,
        "diagnostic": True,
        "rule_ids": ["PORTAL-JDB-002", "PORTAL-JDB-003"],
        "audit": {
            "actor": actor,
            "override_keys": sorted(applied),
            "overrides": applied,
        },
    }
