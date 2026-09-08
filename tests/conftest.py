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
    monkeypatch.setenv("PORTAL_JD_MIN_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("PORTAL_JD_MAX_REQUESTS_PER_SCAN", "1000")
    monkeypatch.setenv("PORTAL_JD_WAF_WAIT_SECONDS", "0")
    # Keep the suite from writing SOP Control receipts into the real product
    # ``.sopcontrol-local/``. Individual adapter tests opt into observe/enforce.
    monkeypatch.setenv(
        "JOBSFLOW_SOPCONTROL_MODE",
        os.environ.get("JOBSFLOW_SOPCONTROL_MODE", "off"),
    )
    browser.reset_portal_budget("jobsdb")
    yield
    browser.reset_portal_budget("jobsdb")
