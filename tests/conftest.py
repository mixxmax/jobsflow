"""Shared test fixtures.

The portal request budget is process-global by design (one budget per scan);
tests isolate it per test so no test inherits another test's counters, and the
default minimum interval is zeroed so fake-browser tests never sleep.
"""

import os

import pytest

from tools.fresh_24h import portal_jd_browser as browser


@pytest.fixture(autouse=True)
def _isolate_jobsdb_budget(monkeypatch):
    # Most browser/recovery tests exercise the official gateway-owned child
    # process. Direct-entry denial is covered explicitly by tests that remove
    # this marker; production code never asks a user/model to set it.
    monkeypatch.setenv("JOBSFLOW_GATEWAY_ACTIVE", "1")
    monkeypatch.setenv("PORTAL_JD_MIN_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("PORTAL_JD_MAX_REQUESTS_PER_SCAN", "1000")
    monkeypatch.setenv("PORTAL_JD_WAF_WAIT_SECONDS", "0")
    # Production forces enforce when a registry exists; the suite must opt into
    # relax so ordinary tests do not mint tickets or write product
    # ``.sopcontrol-local/``. Integration tests set MODE/TICKETS explicitly.
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_ALLOW_RELAX", "1")
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_TEST", "1")
    monkeypatch.setenv(
        "JOBSFLOW_SOPCONTROL_MODE",
        os.environ.get("JOBSFLOW_SOPCONTROL_MODE", "off"),
    )
    monkeypatch.setenv(
        "JOBSFLOW_SOPCONTROL_TICKETS",
        os.environ.get("JOBSFLOW_SOPCONTROL_TICKETS", "off"),
    )
    browser.reset_portal_budget("jobsdb")
    yield
    browser.reset_portal_budget("jobsdb")
