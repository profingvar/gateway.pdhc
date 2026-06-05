"""Spärr Phase 3 (gateway half) — /api/v1/observations block filter
(ticket #206).

Mocks ips.pdhc; the cache is invalidated between tests for isolation.
"""
from unittest.mock import patch
from datetime import datetime, timezone
import uuid

import pytest

from app.models import InboundObservation, ServiceRequestStatus
from app.services import ips_client as ips_mod
from app.services.ips_client import Block


ORG_REQ = str(uuid.uuid4())
PROV_A = str(uuid.uuid4())
PROV_B = str(uuid.uuid4())
CONTRACT = str(uuid.uuid4())
SR = str(uuid.uuid4())
PATIENT = str(uuid.uuid4())


def _seed(db):
    """One contract requested by ORG_REQ, one SR, two observations from
    different provider orgs (PROV_A and PROV_B) on the same patient."""
    db.session.add(ServiceRequestStatus(
        service_request_guid=SR, patient_guid=PATIENT,
        provider_org_guid=PROV_A, contract_guid=CONTRACT,
    ))
    db.session.add(InboundObservation(
        service_request_guid=SR, patient_guid=PATIENT,
        provider_org_guid=PROV_A, contract_guid=CONTRACT,
        concept_guid="c-glucose",
        fhir_observation_json={
            'resourceType': 'Observation', 'id': 'obs-A',
            'subject': {'reference': f'Patient/{PATIENT}'},
            'recorded_at': '2026-05-15T10:00:00+00:00',
        },
    ))
    db.session.add(InboundObservation(
        service_request_guid=SR, patient_guid=PATIENT,
        provider_org_guid=PROV_B, contract_guid=CONTRACT,
        concept_guid="c-glucose",
        fhir_observation_json={
            'resourceType': 'Observation', 'id': 'obs-B',
            'subject': {'reference': f'Patient/{PATIENT}'},
            'recorded_at': '2026-05-15T10:00:00+00:00',
        },
    ))
    db.session.commit()


def _parties_for(contract_guid):
    if contract_guid == CONTRACT:
        return {'requesting_org_guid': ORG_REQ, 'provider_org_guids': [PROV_A, PROV_B]}
    return None


def _blob(admin=True):
    return {
        'user_guid': str(uuid.uuid4()),
        'email': 'tester@local',
        'user_type': 'professional',
        'is_su_admin': admin,
        'effective_phases': ['analysis'],
        'organization_ids': [ORG_REQ],
    }


def _block(scope_id, *, lift_kind=None, lift_concepts=None,
           lift_from=None, lift_until=None, active=True):
    return Block(
        guid=str(uuid.uuid4()),
        patient_guid=PATIENT,
        source_scope_type="clinic",
        source_scope_id=str(scope_id),
        is_active=active,
        lift_kind=lift_kind,
        lift_concept_guids=lift_concepts,
        lift_from_date=lift_from,
        lift_until_date=lift_until,
    )


@pytest.fixture(autouse=True)
def _flush_cache():
    ips_mod._cache.invalidate()
    # Also reset hit/miss counters so each test starts at zero
    ips_mod._cache.hits = 0
    ips_mod._cache.misses = 0
    yield
    ips_mod._cache.invalidate()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_blocked_clinic_ids_filters_to_active_clinic_scopes(self):
        bs = [
            _block("a"),
            _block("b", active=False),
            Block(guid="g", patient_guid="p", source_scope_type="caregiver",
                  source_scope_id="c", is_active=True, lift_kind=None,
                  lift_concept_guids=None, lift_from_date=None, lift_until_date=None),
        ]
        assert ips_mod.blocked_clinic_ids(bs) == {"a"}

    def test_filter_blocked_observations_drops_matching_provider(self):
        class Row:
            def __init__(self, prov):
                self.patient_guid = PATIENT
                self.provider_org_guid = prov
                self.concept_guid = "c"
                self.fhir_observation_json = {}
                self.received_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rows = [Row(PROV_A), Row(PROV_B)]
        blocks = {PATIENT: [_block(PROV_A)]}
        out = ips_mod.filter_blocked_observations(rows, blocks)
        assert [r.provider_org_guid for r in out] == [PROV_B]

    def test_filter_indispensable_care_lift_exposes_concepts(self):
        class Row:
            def __init__(self, concept):
                self.patient_guid = PATIENT
                self.provider_org_guid = PROV_A
                self.concept_guid = concept
                self.fhir_observation_json = {"recorded_at": "2026-05-15T10:00:00+00:00"}
                self.received_at = datetime(2026, 5, 15, 10, tzinfo=timezone.utc)
        rows = [Row("c-exposed"), Row("c-hidden")]
        blocks = {
            PATIENT: [_block(
                PROV_A, lift_kind="indispensable_care",
                lift_concepts=["c-exposed"],
            )]
        }
        out = ips_mod.filter_blocked_observations(rows, blocks)
        assert [r.concept_guid for r in out] == ["c-exposed"]

    def test_filter_date_range_respected_on_lift(self):
        class Row:
            def __init__(self, day):
                self.patient_guid = PATIENT
                self.provider_org_guid = PROV_A
                self.concept_guid = "c1"
                iso = f"2026-05-{day:02d}T10:00:00+00:00"
                self.fhir_observation_json = {"recorded_at": iso}
                self.received_at = datetime(2026, 5, day, 10, tzinfo=timezone.utc)
        rows = [Row(1), Row(15), Row(25)]
        blocks = {
            PATIENT: [_block(
                PROV_A, lift_kind="indispensable_care", lift_concepts=["c1"],
                lift_from="2026-05-10T00:00:00+00:00",
                lift_until="2026-05-20T23:59:59+00:00",
            )]
        }
        out = ips_mod.filter_blocked_observations(rows, blocks)
        assert len(out) == 1
        assert out[0].fhir_observation_json["recorded_at"].startswith("2026-05-15")

    def test_filter_no_blocks_passes_everything(self):
        class Row:
            patient_guid = PATIENT
            provider_org_guid = PROV_A
            concept_guid = "c"
            fhir_observation_json = {}
            received_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rows = [Row(), Row()]
        out = ips_mod.filter_blocked_observations(rows, {})
        assert len(out) == 2


# ---------------------------------------------------------------------------
# Cache behaviour + stats
# ---------------------------------------------------------------------------

class TestCache:
    def test_cache_hit_within_ttl(self, app):
        calls = {"n": 0}

        class Fake:
            def fetch_active_blocks(self, p):
                calls["n"] += 1
                return [_block(PROV_A)]

        with app.app_context():
            ips_mod.get_active_blocks(PATIENT, client=Fake())
            ips_mod.get_active_blocks(PATIENT, client=Fake())
            ips_mod.get_active_blocks(PATIENT, client=Fake())
        assert calls["n"] == 1
        stats = ips_mod.cache_stats()
        assert stats["hits"] == 2 and stats["misses"] == 1

    def test_invalidate_evicts(self, app):
        calls = {"n": 0}

        class Fake:
            def fetch_active_blocks(self, p):
                calls["n"] += 1
                return []

        with app.app_context():
            ips_mod.get_active_blocks(PATIENT, client=Fake())
            ips_mod.invalidate(PATIENT)
            ips_mod.get_active_blocks(PATIENT, client=Fake())
        assert calls["n"] == 2

    def test_fetch_blocks_for_patients_dedupes(self, app):
        calls = []

        class Fake:
            def fetch_active_blocks(self, p):
                calls.append(p)
                return []

        p1 = str(uuid.uuid4())
        p2 = str(uuid.uuid4())
        with app.app_context():
            out = ips_mod.fetch_blocks_for_patients(
                [p1, p2, p1, p2, p1], client=Fake(),
            )
        assert set(out.keys()) == {p1, p2}
        assert sorted(calls) == sorted([p1, p2])


# ---------------------------------------------------------------------------
# End-to-end through /api/v1/observations
# ---------------------------------------------------------------------------

class TestEndpoint:
    def test_observations_endpoint_drops_blocked_rows(self, client, db):
        _seed(db)
        with patch('app.api.observations.validate_sso_token', return_value=_blob()), \
             patch(
                 'app.api.observations.ContractScopeService.fetch_parties',
                 side_effect=_parties_for,
             ), \
             patch(
                 'app.api.observations.fetch_blocks_for_patients',
                 return_value={PATIENT: [_block(PROV_A)]},
             ):
            r = client.get(
                f'/api/v1/observations?organization={ORG_REQ}',
                headers={'Authorization': 'Bearer good'},
            )
        assert r.status_code == 200
        j = r.get_json()
        # PROV_A blocked, PROV_B passes
        assert j['total'] == 1
        assert j['entry'][0]['resource']['id'] == 'obs-B'

    def test_observations_endpoint_passes_through_without_blocks(self, client, db):
        _seed(db)
        with patch('app.api.observations.validate_sso_token', return_value=_blob()), \
             patch(
                 'app.api.observations.ContractScopeService.fetch_parties',
                 side_effect=_parties_for,
             ), \
             patch(
                 'app.api.observations.fetch_blocks_for_patients',
                 return_value={},
             ):
            r = client.get(
                f'/api/v1/observations?organization={ORG_REQ}',
                headers={'Authorization': 'Bearer good'},
            )
        assert r.status_code == 200
        j = r.get_json()
        assert j['total'] == 2

    def test_observations_endpoint_all_rows_blocked_returns_empty_bundle(self, client, db):
        _seed(db)
        with patch('app.api.observations.validate_sso_token', return_value=_blob()), \
             patch(
                 'app.api.observations.ContractScopeService.fetch_parties',
                 side_effect=_parties_for,
             ), \
             patch(
                 'app.api.observations.fetch_blocks_for_patients',
                 return_value={PATIENT: [_block(PROV_A), _block(PROV_B)]},
             ):
            r = client.get(
                f'/api/v1/observations?organization={ORG_REQ}',
                headers={'Authorization': 'Bearer good'},
            )
        assert r.status_code == 200
        j = r.get_json()
        assert j['total'] == 0
        assert j['entry'] == []

    def test_indispensable_care_lift_exposes_concepts_e2e(self, client, db):
        _seed(db)
        lift_block = _block(
            PROV_A, lift_kind="indispensable_care",
            lift_concepts=["c-glucose"],
        )
        with patch('app.api.observations.validate_sso_token', return_value=_blob()), \
             patch(
                 'app.api.observations.ContractScopeService.fetch_parties',
                 side_effect=_parties_for,
             ), \
             patch(
                 'app.api.observations.fetch_blocks_for_patients',
                 return_value={PATIENT: [lift_block]},
             ):
            r = client.get(
                f'/api/v1/observations?organization={ORG_REQ}',
                headers={'Authorization': 'Bearer good'},
            )
        assert r.status_code == 200
        # Both rows pass: PROV_B isn't blocked at all, PROV_A is blocked
        # but lifted for c-glucose specifically.
        assert r.get_json()['total'] == 2
