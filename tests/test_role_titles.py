from tools.job_materials.role_titles import (
    ROLE_TITLE_PARSER_VERSION,
    build_role_title_contract,
    normalize_role_for_material,
)


def test_substantive_parenthetical_is_preserved_and_metadata_is_not():
    substantive = build_role_title_contract("Paralegal (Corporate Funds)")
    metadata = build_role_title_contract("Paralegal (Hong Kong)")
    fullwidth = build_role_title_contract("分析师（香港）")

    assert substantive["primary"] == "Paralegal (Corporate Funds)"
    assert substantive["specialisms"] == ["Corporate Funds"]
    assert metadata["primary"] == "Paralegal"
    assert metadata["metadata_parentheticals"] == ["Hong Kong"]
    assert fullwidth["primary"] == "分析师"


def test_slash_alternatives_are_separate_but_known_acronym_compound_is_not():
    alternatives = build_role_title_contract("Paralegal / Legal Assistant")
    acronym = build_role_title_contract("UI/UX Designer")

    assert alternatives["primary"] == "Paralegal"
    assert alternatives["alternates"] == ["Legal Assistant"]
    assert alternatives["confirmation_needed"] is True
    assert acronym["primary"] == "UI/UX Designer"
    assert acronym["alternates"] == []


def test_normalizer_never_introduces_a_short_dash_for_parentheses():
    value = normalize_role_for_material("Paralegal (Corporate Funds)")
    assert value == "Paralegal (Corporate Funds)"
    assert "-" not in value


def test_compliance_and_design_compounds_are_one_role():
    for title in (
        "KYC/CDD Officer (HNW Client)",
        "AML/KYC Analyst",
        "UI/UX Designer",
    ):
        contract = build_role_title_contract(title)
        assert contract["primary"] == title
        assert contract["alternates"] == []
        assert contract["ambiguity_status"] == "not_ambiguous"


def test_salary_parenthetical_is_metadata_and_not_part_of_outbound_role():
    contract = build_role_title_contract("KYC/CDD Officer (HNW Client) (Up to $30K)")

    assert contract["parser_version"] == ROLE_TITLE_PARSER_VERSION
    assert contract["primary"] == "KYC/CDD Officer (HNW Client)"
    assert contract["specialisms"] == ["HNW Client"]
    assert contract["metadata_parentheticals"] == ["Up to $30K"]
    assert contract["ambiguity_status"] == "not_ambiguous"


def test_true_multiple_roles_require_confirmation_until_user_selects_one():
    pending = build_role_title_contract("Paralegal / Legal Assistant")
    confirmed = build_role_title_contract(
        "Paralegal / Legal Assistant", selected_primary="Legal Assistant"
    )

    assert pending["ambiguity_status"] == "pending_confirmation"
    assert confirmed["ambiguity_status"] == "user_confirmed"
    assert confirmed["confirmation_needed"] is False
