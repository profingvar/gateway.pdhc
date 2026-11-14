"""M0 #418 — gateway adopts affiliations[] Zone-1 scope on receive-phase reads.

The observations read endpoint scopes org access via scope_org_guids(blob),
and has_analysis_access reads session_phases with a legacy fallback.
"""
from app.services.sso_service import scope_org_guids, has_analysis_access


def test_scope_from_affiliations():
    blob = {"affiliations": [{"care_unit_guid": "u1"}, {"care_unit_guid": "u2"}]}
    assert scope_org_guids(blob) == ["u1", "u2"]


def test_scope_precedence_over_legacy():
    blob = {"affiliations": [{"care_unit_guid": "u1"}],
            "organization_ids": ["other"]}
    assert scope_org_guids(blob) == ["u1"]


def test_legacy_organization_ids_ALONE_grant_no_scope():
    """2026-11-14: this test used to PIN the fallback (organization_ids ->
    scope). Memberships are not care-unit assignments; no affiliation, no
    scope."""
    assert scope_org_guids({"organization_ids": ["o1"]}) == []
    assert scope_org_guids({"affiliations": [], "organization_ids": ["o1"]}) == []


def test_scope_empty_and_none():
    assert scope_org_guids({}) == []
    assert scope_org_guids(None) == []


def test_analysis_gate_prefers_session_phases():
    assert has_analysis_access(
        {"user_type": "professional", "session_phases": ["analysis"],
         "effective_phases": []}) is True


def test_analysis_gate_legacy_fallback():
    assert has_analysis_access(
        {"user_type": "professional", "effective_phases": ["analysis"]}) is True


def test_analysis_gate_denies():
    assert has_analysis_access(
        {"user_type": "professional", "session_phases": ["provider"]}) is False
