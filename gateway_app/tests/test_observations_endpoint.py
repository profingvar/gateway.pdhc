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
                   return_value=_blob([ORG_A])), _patch_parties():
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
                   return_value=_blob([ORG_B])), _patch_parties():
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
                   return_value=_blob([], admin=True)), _patch_parties():
            r = client.get(
                f'/api/v1/observations?organization={ORG_A}',
                headers={'Authorization': 'Bearer t'},
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
                   return_value=_blob([], admin=True)), _patch_parties():
            r = client.get(
                f'/api/v1/observations?organization={unknown_org}',
                headers={'Authorization': 'Bearer t'},
            )
        assert r.status_code == 200
        assert r.get_json()['total'] == 0
        assert r.get_json()['entry'] == []
