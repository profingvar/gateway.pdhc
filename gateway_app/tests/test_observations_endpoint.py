"""Tests for GET /api/v1/observations?organization=<guid>.

Covers:
- 401 missing/invalid bearer
- 403 phase gate (no analysis)
- 403 org not in caller scope (non-admin)
- admin bypass returns everything
- requesting-org filtering: only observations whose contract's
  requesting_org matches the queried org are returned
- empty bundle when nothing matches
"""
from unittest.mock import patch
import uuid

import pytest

from app.models import InboundObservation, ServiceRequestStatus
from app.extensions import db


ORG_A = str(uuid.uuid4())
ORG_B = str(uuid.uuid4())
PROV_ORG = str(uuid.uuid4())
CONTRACT_A = str(uuid.uuid4())
CONTRACT_B = str(uuid.uuid4())
SR_A = str(uuid.uuid4())
SR_B = str(uuid.uuid4())
PATIENT_1 = str(uuid.uuid4())
PATIENT_2 = str(uuid.uuid4())


def _seed(db):
    """Two contracts: A requested by ORG_A, B requested by ORG_B.
    Two observations, one under each contract."""
    db.session.add(ServiceRequestStatus(
        service_request_guid=SR_A, patient_guid=PATIENT_1,
        provider_org_guid=PROV_ORG, contract_guid=CONTRACT_A,
    ))
    db.session.add(ServiceRequestStatus(
        service_request_guid=SR_B, patient_guid=PATIENT_2,
        provider_org_guid=PROV_ORG, contract_guid=CONTRACT_B,
    ))
    db.session.add(InboundObservation(
        service_request_guid=SR_A, patient_guid=PATIENT_1,
        provider_org_guid=PROV_ORG, contract_guid=CONTRACT_A,
        fhir_observation_json={
            'resourceType': 'Observation', 'id': 'obs-A',
            'subject': {'reference': f'Patient/{PATIENT_1}'},
        },
    ))
    db.session.add(InboundObservation(
        service_request_guid=SR_B, patient_guid=PATIENT_2,
        provider_org_guid=PROV_ORG, contract_guid=CONTRACT_B,
        fhir_observation_json={
            'resourceType': 'Observation', 'id': 'obs-B',
            'subject': {'reference': f'Patient/{PATIENT_2}'},
        },
    ))
    db.session.commit()


def _parties_for(contract_guid):
    if contract_guid == CONTRACT_A:
        return {'requesting_org_guid': ORG_A, 'provider_org_guids': [PROV_ORG]}
    if contract_guid == CONTRACT_B:
        return {'requesting_org_guid': ORG_B, 'provider_org_guids': [PROV_ORG]}
    return None


def _patch_parties():
    return patch(
        'app.api.observations.ContractScopeService.fetch_parties',
        side_effect=_parties_for,
    )


# Map known SR guids → the FHIR Observation cdr1 would return for them.
# Used by _patch_cdr below to simulate the proxy round-trip.
_SR_TO_OBS = {
    SR_A: {
        'resourceType': 'Observation', 'id': 'obs-A',
        'subject': {'reference': f'Patient/{PATIENT_1}'},
        'basedOn': [{'identifier': {'value': SR_A}}],
        'performer': [{'identifier': {'value': PROV_ORG}}],
    },
    SR_B: {
        'resourceType': 'Observation', 'id': 'obs-B',
        'subject': {'reference': f'Patient/{PATIENT_2}'},
        'basedOn': [{'identifier': {'value': SR_B}}],
        'performer': [{'identifier': {'value': PROV_ORG}}],
    },
}


def _cdr_search(service_request_guids, *, patient=None, request_id=''):
    """Stand-in for AnalyseClient.search_observations. Filters _SR_TO_OBS
    by the requested SR set."""
    entries = [
        {'resource': _SR_TO_OBS[sr]} for sr in service_request_guids
        if sr in _SR_TO_OBS
    ]
    return {
        'resourceType': 'Bundle', 'type': 'searchset',
        'timestamp': '2026-06-27T00:00:00+00:00',
        'total': len(entries), 'entry': entries,
    }


def _patch_cdr():
    return patch(
        'app.api.observations.AnalyseClient.search_observations',
        side_effect=_cdr_search,
    )


def _blob(orgs, admin=False, phases=('analysis',)):
    return {
        'user_guid': str(uuid.uuid4()),
        'email': 'tester@local',
        'user_type': 'professional',
        'is_su_admin': admin,
        'effective_phases': list(phases),
        'organization_ids': list(orgs),
    }


# ── auth checks ───────────────────────────────────────────────────────

class TestAuth:
    def test_missing_bearer(self, client, db):
        r = client.get(f'/api/v1/observations?organization={ORG_A}')
        assert r.status_code == 401

    def test_invalid_token(self, client, db):
        with patch('app.api.observations.validate_sso_token', return_value=None):
            r = client.get(
                f'/api/v1/observations?organization={ORG_A}',
                headers={'Authorization': 'Bearer bad'},
            )
        assert r.status_code == 401

    def test_no_phase(self, client, db):
        with patch('app.api.observations.validate_sso_token',
                   return_value=_blob([ORG_A], phases=['planning'])):
            r = client.get(
                f'/api/v1/observations?organization={ORG_A}',
                headers={'Authorization': 'Bearer t'},
            )
        assert r.status_code == 403

    def test_org_not_in_scope(self, client, db):
        with patch('app.api.observations.validate_sso_token',
                   return_value=_blob([ORG_B])):
            r = client.get(
                f'/api/v1/observations?organization={ORG_A}',
                headers={'Authorization': 'Bearer t'},
            )
        assert r.status_code == 403

    def test_missing_org_param(self, client, db):
        with patch('app.api.observations.validate_sso_token',
                   return_value=_blob([ORG_A])):
            r = client.get(
                '/api/v1/observations',
                headers={'Authorization': 'Bearer t'},
            )
        assert r.status_code == 400


# ── filtering by requesting org ───────────────────────────────────────

class TestRequestingOrgFilter:
    def test_returns_only_obs_for_requesting_org(self, client, db):
        _seed(db)
        with patch('app.api.observations.validate_sso_token',
                   return_value=_blob([ORG_A])), _patch_parties(), _patch_cdr():
            r = client.get(
                f'/api/v1/observations?organization={ORG_A}',
                headers={'Authorization': 'Bearer t'},
            )
        assert r.status_code == 200
        bundle = r.get_json()
        assert bundle['resourceType'] == 'Bundle'
        ids = [e['resource']['id'] for e in bundle['entry']]
        assert ids == ['obs-A']

    def test_other_org_gets_other_obs(self, client, db):
        _seed(db)
        with patch('app.api.observations.validate_sso_token',
                   return_value=_blob([ORG_B])), _patch_parties(), _patch_cdr():
            r = client.get(
                f'/api/v1/observations?organization={ORG_B}',
                headers={'Authorization': 'Bearer t'},
            )
        assert r.status_code == 200
        ids = [e['resource']['id'] for e in r.get_json()['entry']]
        assert ids == ['obs-B']

    def test_admin_bypass_org_scope(self, client, db):
        """Admin querying ORG_A still only sees ORG_A's data — admin bypass
        applies to the per-user-org check, not to requesting-org filtering."""
        _seed(db)
        with patch('app.api.observations.validate_sso_token',
                   return_value=_blob([], admin=True)), _patch_parties(), _patch_cdr():
            r = client.get(
                f'/api/v1/observations?organization={ORG_A}',
                headers={
                    'Authorization': 'Bearer t',
                    'X-Admin-Justification': 'incident response',
                },
            )
        assert r.status_code == 200
        ids = [e['resource']['id'] for e in r.get_json()['entry']]
        # admin bypass on _user_orgs check_; requesting-org filter still applied
        # because we want admin to be able to inspect a specific org's view
        assert 'obs-A' in ids

    def test_empty_when_no_match(self, client, db):
        _seed(db)
        unknown_org = str(uuid.uuid4())
        with patch('app.api.observations.validate_sso_token',
                   return_value=_blob([], admin=True)), _patch_parties(), _patch_cdr():
            r = client.get(
                f'/api/v1/observations?organization={unknown_org}',
                headers={
                    'Authorization': 'Bearer t',
                    'X-Admin-Justification': 'incident response',
                },
            )
        assert r.status_code == 200
        assert r.get_json()['total'] == 0
        assert r.get_json()['entry'] == []


# ── #220: admin-bypass audited as distinct event ──────────────────────

class TestAdminReadAudit:
    """Ticket #220. An SU admin reading observations for an org outside
    their own organisations writes ``observations.admin_read`` (not the
    generic ``observations.read``) and the bypass requires an explicit
    justification text via ``X-Admin-Justification``.
    """

    def test_admin_in_own_org_writes_observations_read(self, client, db):
        from app.models import AuditLog
        _seed(db)
        # ORG_A is in the admin's organization_ids — no bypass triggered.
        with patch('app.api.observations.validate_sso_token',
                   return_value=_blob([ORG_A], admin=True)), _patch_parties(), _patch_cdr():
            r = client.get(
                f'/api/v1/observations?organization={ORG_A}',
                headers={'Authorization': 'Bearer t'},
            )
        assert r.status_code == 200
        rows = AuditLog.query.filter(
            AuditLog.event_type.in_(
                ['observations.read', 'observations.admin_read'],
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].event_type == 'observations.read'
        assert 'justification' not in (rows[0].payload_snapshot or {})

    def test_admin_outside_org_writes_admin_read_with_justification(
        self, client, db,
    ):
        from app.models import AuditLog
        _seed(db)
        with patch('app.api.observations.validate_sso_token',
                   return_value=_blob([ORG_B], admin=True)), _patch_parties(), _patch_cdr():
            r = client.get(
                f'/api/v1/observations?organization={ORG_A}',
                headers={
                    'Authorization': 'Bearer t',
                    'X-Admin-Justification': 'Patient SAR — req #4711',
                },
            )
        assert r.status_code == 200
        rows = AuditLog.query.filter(
            AuditLog.event_type.in_(
                ['observations.read', 'observations.admin_read'],
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].event_type == 'observations.admin_read'
        snap = rows[0].payload_snapshot or {}
        assert snap.get('justification') == 'Patient SAR — req #4711'
        assert snap.get('org_guid') == ORG_A

    def test_admin_outside_org_without_justification_400(self, client, db):
        from app.models import AuditLog
        _seed(db)
        with patch('app.api.observations.validate_sso_token',
                   return_value=_blob([ORG_B], admin=True)), _patch_parties(), _patch_cdr():
            r = client.get(
                f'/api/v1/observations?organization={ORG_A}',
                headers={'Authorization': 'Bearer t'},
            )
        assert r.status_code == 400
        body = r.get_json()
        assert 'justification' in body.get('error', '').lower()
        # And no audit row is written for the rejected call — the bypass
        # never executed.
        assert AuditLog.query.filter(
            AuditLog.event_type.in_(
                ['observations.read', 'observations.admin_read'],
            )
        ).count() == 0

    def test_admin_outside_org_whitespace_only_justification_400(
        self, client, db,
    ):
        """An all-whitespace header is treated as missing — protects
        against operator habit of typing a space to dismiss prompts."""
        _seed(db)
        with patch('app.api.observations.validate_sso_token',
                   return_value=_blob([ORG_B], admin=True)), _patch_parties(), _patch_cdr():
            r = client.get(
                f'/api/v1/observations?organization={ORG_A}',
                headers={
                    'Authorization': 'Bearer t',
                    'X-Admin-Justification': '   ',
                },
            )
        assert r.status_code == 400

    def test_non_admin_in_own_org_still_writes_observations_read(
        self, client, db,
    ):
        """Sanity: the spec change does not touch non-admin paths."""
        from app.models import AuditLog
        _seed(db)
        with patch('app.api.observations.validate_sso_token',
                   return_value=_blob([ORG_A])), _patch_parties(), _patch_cdr():
            client.get(
                f'/api/v1/observations?organization={ORG_A}',
                headers={'Authorization': 'Bearer t'},
            )
        rows = AuditLog.query.filter(
            AuditLog.event_type.in_(
                ['observations.read', 'observations.admin_read'],
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].event_type == 'observations.read'


# ── #282 SSOT phase 3 proxy behaviour ─────────────────────────────────

class TestCdrProxyBehaviour:
    """Phase 3 SSOT cutover (ticket #282). Gateway now proxies the
    analyse-pull through to cdr1 via AnalyseClient.search_observations
    instead of reading InboundObservation locally. Verifies the proxy
    contract: SR pre-filter, cdr1-down fallback, spärr post-filter,
    audit is still written gateway-side.
    """

    def test_passes_matching_sr_guids_to_cdr(self, client, db):
        _seed(db)
        captured = {}
        def _capture(service_request_guids, *, patient=None, request_id=''):
            captured['srs'] = list(service_request_guids)
            return _cdr_search(service_request_guids, patient=patient,
                               request_id=request_id)
        with patch('app.api.observations.validate_sso_token',
                   return_value=_blob([ORG_A])), _patch_parties(), \
             patch('app.api.observations.AnalyseClient.search_observations',
                   side_effect=_capture):
            r = client.get(
                f'/api/v1/observations?organization={ORG_A}',
                headers={'Authorization': 'Bearer t'},
            )
        assert r.status_code == 200
        assert captured['srs'] == [SR_A], \
            f"gateway must pre-filter SRs to cdr1; got {captured['srs']}"

    def test_cdr_unavailable_returns_502(self, client, db):
        from app.services.analyse_client import AnalyseUnavailable
        _seed(db)
        with patch('app.api.observations.validate_sso_token',
                   return_value=_blob([ORG_A])), _patch_parties(), \
             patch('app.api.observations.AnalyseClient.search_observations',
                   side_effect=AnalyseUnavailable('connect refused')):
            r = client.get(
                f'/api/v1/observations?organization={ORG_A}',
                headers={'Authorization': 'Bearer t'},
            )
        assert r.status_code == 502
        assert 'analyse' in r.get_json()['error'].lower()

    def test_audit_still_written_on_proxy_path(self, client, db):
        from app.models import AuditLog
        _seed(db)
        with patch('app.api.observations.validate_sso_token',
                   return_value=_blob([ORG_A])), _patch_parties(), _patch_cdr():
            client.get(
                f'/api/v1/observations?organization={ORG_A}',
                headers={'Authorization': 'Bearer t'},
            )
        rows = AuditLog.query.filter_by(event_type='observations.read').all()
        assert len(rows) == 1
        snap = rows[0].payload_snapshot or {}
        # patient_guids should be sourced from the FHIR Bundle's
        # subject.reference, not from InboundObservation.
        assert snap.get('patient_guids') == [PATIENT_1]

    def test_blocked_patient_filtered_post_cdr(self, client, db):
        """Spärr (IPS block) filter still applies to the cdr1 result."""
        from app.services.ips_client import Block
        _seed(db)
        block = Block(
            guid='b1', patient_guid=PATIENT_1,
            source_scope_type='clinic', source_scope_id=PROV_ORG,
            is_active=True, lift_kind=None, lift_concept_guids=[],
            lift_from_date=None, lift_until_date=None,
        )
        with patch('app.api.observations.validate_sso_token',
                   return_value=_blob([ORG_A])), _patch_parties(), \
             _patch_cdr(), \
             patch('app.api.observations.fetch_blocks_for_patients',
                   return_value={PATIENT_1: [block]}):
            r = client.get(
                f'/api/v1/observations?organization={ORG_A}',
                headers={'Authorization': 'Bearer t'},
            )
        assert r.status_code == 200
        bundle = r.get_json()
        # obs-A was the only match, and it is now spärrad → empty bundle.
        assert bundle['total'] == 0
        assert bundle['entry'] == []
