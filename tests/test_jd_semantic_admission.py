"""Profession-neutral regressions for sparse, heading-free job descriptions."""

import pytest

from tools.fresh_24h import portal_jd_browser as browser


HEADERLESS_BODY = (
    "Experience supporting operational requests and maintaining project records.\n\n"
    "Clear written communication and careful attention to detail.\n\n"
    "At least one year's working experience in a related setting is preferred.\n\n"
    "The role follows a five-day schedule in a collaborative team and includes "
    "ownership of assigned documentation."
)


@pytest.mark.parametrize("body", [
    "Requirements\n" + "Maintain accurate records and coordinate assigned projects. " * 6,
    HEADERLESS_BODY,
])
def test_one_job_signal_is_sufficient_in_a_trusted_container(body):
    assert browser.MIN_BODY_CHARS <= len(body) < 400
    assert browser.is_real_jd(
        title="Project Coordinator", body=body, html_snip="",
        has_jd_container=True, cf_mitigated=None,
    )


@pytest.mark.parametrize("overrides", [
    {"has_jd_container": False},
    {"cf_mitigated": "challenge"},
    {"title": "Just a moment..."},
    {"body": "Requirements: brief"},
    {"body": "Our organization serves customers around the world. " * 8},
])
def test_one_signal_policy_keeps_structural_waf_and_empty_guards(overrides):
    args = dict(title="Project Coordinator", body=HEADERLESS_BODY,
                html_snip="", has_jd_container=True, cf_mitigated=None)
    args.update(overrides)
    assert not browser.is_real_jd(**args)
