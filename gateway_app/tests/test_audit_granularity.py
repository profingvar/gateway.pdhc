"""Ticket #221 — Gateway PDL #3: read-side audit granularity.

Pins the decision matrix shipped in gateway_technical_guide.md:

- ``observations.read`` (normal scope) — ONE row per query carrying
  the full ``patient_guids[]`` list in payload_snapshot. Kontroller
  can query "was P in any read by X" via JSONB membership.
- ``observations.admin_read`` (off-org bypass, #220) — ONE row per
  patient touched. Each carries the same ``justification`` verbatim
  and the same ``correlation_id`` so the bypass act stays joinable.
- ``provider.feed.polled`` — ONE row per call (metadata only;
  per-patient rows on a 30s poll cadence would overwhelm the audit
  table for no kontroller gain).

The pre-existing audit tests in test_observations_endpoint cover the
event-type split (#220); these tests pin the granularity *shape* the
new decision matrix locks in.
"""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import AuditLog, InboundObservation, ServiceRequestStatus


# Reuse the seed pattern from test_observations_endpoint so the
# fixtures aren't drifted apart.

ORG_A = str(uuid.uuid4())
ORG_B = str(uuid.uuid4())
PROV_ORG = str(uuid.uuid4())
CONTRACT_A = str(uuid.uuid4())
SR_A = str(uuid.uuid4())
SR_C = str(uuid.uuid4())
PATIENT_1 = str(uuid.uuid4())
PATIENT_2 = str(uuid.uuid4())
PATIENT_3 = str(uuid.uuid4())


def _seed_multi_patient(db):
    """One contract, three SRs across three patients — exercises the
    per-patient explode path under admin bypass."""
    for sr, pat in (
        (SR_A, PATIENT_1),
        (SR_C, PATIENT_2),
        # Third row under same SR but different patient — possible in
        # provider data shapes.
        (None, PATIENT_3),
    ):
        if sr:
            db.session.add(ServiceRequestStatus(
                service_request_guid=sr,
                patient_guid=pat,
                provider_org_guid=PROV_ORG,
                contract_guid=CONTRACT_A,
            ))
    # Three observations, each pinned to one of the above patients.
    for sr_guid, pg in (
        (SR_A, PATIENT_1),
        (SR_C, PATIENT_2),
        (SR_A, PATIENT_3),
    ):
        db.session.add(InboundObservation(
            service_request_guid=sr_guid, patient_guid=pg,
            provider_org_guid=PROV_ORG, contract_guid=CONTRACT_A,
            fhir_observation_json={
                'resourceType': 'Observation',
                'id': f'obs-{pg[:6]}',
                'subject': {'reference': f'Patient/{pg}'},
            },
        ))
    db.session.commit()


def _patch_parties():
    return patch(
        'app.api.observations.ContractScopeService.fetch_parties',
        side_effect=lambda c: {
            'requesting_org_guid': ORG_A,
            'provider_org_guids': [PROV_ORG],
        } if c == CONTRACT_A else None,
    )


# Phase 3 SSOT (#282): gateway proxies analyse-pull to cdr1. The audit
# tests only care about audit shape; the bundle they would have
# observed must come back through the mocked CdrClient.
def _cdr_search(service_request_guids, *, patient=None, request_id=''):
    sr_to_patients = {SR_A: [PATIENT_1, PATIENT_3], SR_C: [PATIENT_2]}
    entries = []
    for sr in service_request_guids:
        for pg in sr_to_patients.get(sr, []):
            entries.append({'resource': {
                'resourceType': 'Observation',
                'id': f'obs-{pg[:6]}',
                'subject': {'reference': f'Patient/{pg}'},
                'basedOn': [{'identifier': {'value': sr}}],
                'performer': [{'identifier': {'value': PROV_ORG}}],
            }})
    return {
        'resourceType': 'Bundle', 'type': 'searchset',
        'timestamp': '2026-06-27T00:00:00+00:00',
        'total': len(entries), 'entry': entries,
    }


def _patch_cdr():
    return patch(
        'app.api.observations.CdrClient.search_observations',
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


# ---------------------------------------------------------------------------
# /observations normal scope — per-query with patient_guids[]
# ---------------------------------------------------------------------------

class TestObservationsReadPerQuery:
    def test_one_row_with_full_patient_list(self, client, db):
        _seed_multi_patient(db)
        with patch(
            'app.api.observations.validate_sso_token',
            return_value=_blob([ORG_A], admin=False),
        ), _patch_parties(), _patch_cdr():
            r = client.get(
                f'/api/v1/observations?organization={ORG_A}',
                headers={'Authorization': 'Bearer t'},
            )
        assert r.status_code == 200
        rows = AuditLog.query.filter_by(
            event_type='observations.read',
        ).all()
        assert len(rows) == 1, "per-query: exactly one row"
        snap = rows[0].payload_snapshot or {}
        assert snap.get('granularity') == 'per-query'
        assert snap.get('n_patients') == 3
        assert sorted(snap.get('patient_guids') or []) == sorted([
            PATIENT_1, PATIENT_2, PATIENT_3,
        ])

    def test_no_observations_still_writes_one_row(self, client, db):
        # No InboundObservation rows seeded; only orgs.
        with patch(
            'app.api.observations.validate_sso_token',
            return_value=_blob([ORG_A], admin=False),
        ), _patch_parties(), _patch_cdr():
            r = client.get(
                f'/api/v1/observations?organization={ORG_A}',
                headers={'Authorization': 'Bearer t'},
            )
        assert r.status_code == 200
        rows = AuditLog.query.filter_by(
            event_type='observations.read',
        ).all()
        assert len(rows) == 1
        snap = rows[0].payload_snapshot or {}
        assert snap.get('n_patients') == 0
        assert snap.get('patient_guids') == []


# ---------------------------------------------------------------------------
# /observations admin off-org bypass — per-patient explode
# ---------------------------------------------------------------------------

class TestObservationsAdminReadPerPatient:
    def test_admin_bypass_writes_one_row_per_patient(self, client, db):
        _seed_multi_patient(db)
        # SU admin reading ORG_A while affiliated to ORG_B (off-org
        # bypass — must include X-Admin-Justification, #220).
        with patch(
            'app.api.observations.validate_sso_token',
            return_value=_blob([ORG_B], admin=True),
        ), _patch_parties(), _patch_cdr():
            r = client.get(
                f'/api/v1/observations?organization={ORG_A}',
                headers={
                    'Authorization': 'Bearer t',
                    'X-Admin-Justification': 'kontroll #7711',
                    'X-Correlation-Id': 'corr-1',
                },
            )
        assert r.status_code == 200
        rows = AuditLog.query.filter_by(
            event_type='observations.admin_read',
        ).all()
        # Three patients in the result -> three audit rows.
        assert len(rows) == 3
        patient_ids = sorted(
            (r.payload_snapshot or {}).get('patient_guid')
            for r in rows
        )
        assert patient_ids == sorted([PATIENT_1, PATIENT_2, PATIENT_3])
        # Same justification + correlation_id on every row.
        for row in rows:
            snap = row.payload_snapshot or {}
            assert snap.get('granularity') == 'per-patient'
            assert snap.get('justification') == 'kontroll #7711'
            assert snap.get('n_patients') == 3
            assert row.correlation_id == 'corr-1'

    def test_admin_bypass_zero_patients_still_writes_row(self, client, db):
        """Even when count==0 the bypass act is recorded. Without this
        a kontroller can't tell whether the admin tried-and-got-nothing
        or never tried at all."""
        with patch(
            'app.api.observations.validate_sso_token',
            return_value=_blob([ORG_B], admin=True),
        ), _patch_parties(), _patch_cdr():
            r = client.get(
                f'/api/v1/observations?organization={ORG_A}',
                headers={
                    'Authorization': 'Bearer t',
                    'X-Admin-Justification': 'kontroll #7712',
                },
            )
        assert r.status_code == 200
        rows = AuditLog.query.filter_by(
            event_type='observations.admin_read',
        ).all()
        assert len(rows) == 1
        snap = rows[0].payload_snapshot or {}
        assert snap.get('patient_guid') is None
        assert snap.get('n_patients') == 0
        assert snap.get('justification') == 'kontroll #7712'

    def test_kontroller_can_filter_by_patient_via_jsonb(
        self, client, db,
    ):
        """The decision matrix promises kontroller can answer
        'show every read of patient P' from the audit_log table alone.
        Verify both granularities produce a queryable answer."""
        _seed_multi_patient(db)
        # First a normal read (per-query row with patient_guids[]).
        with patch(
            'app.api.observations.validate_sso_token',
            return_value=_blob([ORG_A], admin=False),
        ), _patch_parties(), _patch_cdr():
            client.get(
                f'/api/v1/observations?organization={ORG_A}',
                headers={'Authorization': 'Bearer t'},
            )
        # Then an admin bypass (per-patient rows).
        with patch(
            'app.api.observations.validate_sso_token',
            return_value=_blob([ORG_B], admin=True),
        ), _patch_parties(), _patch_cdr():
            client.get(
                f'/api/v1/observations?organization={ORG_A}',
                headers={
                    'Authorization': 'Bearer t',
                    'X-Admin-Justification': 'kontroll',
                },
            )
        # Now kontroller looks for "every audit row that touched
        # PATIENT_1". On SQLite the JSON operator dialect differs, so
        # we exercise the equivalent: per-query row's patient_guids
        # contains PATIENT_1; per-patient row's patient_guid equals
        # PATIENT_1.
        per_query = AuditLog.query.filter_by(
            event_type='observations.read',
        ).all()
        assert any(
            PATIENT_1 in (r.payload_snapshot or {}).get('patient_guids', [])
            for r in per_query
        )
        per_patient = AuditLog.query.filter_by(
            event_type='observations.admin_read',
        ).all()
        assert any(
            (r.payload_snapshot or {}).get('patient_guid') == PATIENT_1
            for r in per_patient
        )


# ---------------------------------------------------------------------------
# /provider/feed — per-query (was missing entirely pre-#221)
# ---------------------------------------------------------------------------

class TestProviderFeedAudit:
    def test_feed_call_writes_per_query_audit(self, client, db):
        """The feed proxies to request.pdhc; we stub that out and
        verify the gateway-side audit row records the poll."""
        with patch(
            'app.api.provider.require_provider_token',
            lambda **kw: (lambda f: f),
        ):
            pass  # decorator is module-level; mock pathway is different

        # Easier: mock the auth + service.
        org_guid = str(uuid.uuid4())
        with patch(
            'app.api.auth.PATValidationService.validate'
        ) as auth_mock, patch(
            'app.api.provider.FeedService.get_feed',
            return_value=(
                {'resourceType': 'Bundle', 'entry': [
                    {'resource': {'id': 'sr-1'}},
                    {'resource': {'id': 'sr-2'}},
                ]},
                200,
            ),
        ):
            from app.services.pat_validation import PATValidationResult
            auth_mock.return_value = PATValidationResult(
                valid=True,
                provider_org_guid=org_guid,
                scopes='read',
                error=None,
            )
            r = client.get(
                '/api/v1/provider/feed?since=2026-06-01T00:00:00Z&limit=50',
                headers={'X-Provider-Token': 'pat-token'},
            )
        assert r.status_code == 200
        rows = AuditLog.query.filter_by(
            event_type='provider.feed.polled',
        ).all()
        assert len(rows) == 1
        snap = rows[0].payload_snapshot or {}
        assert snap.get('granularity') == 'per-query'
        assert snap.get('n_items') == 2
        assert snap.get('since') == '2026-06-01T00:00:00Z'
        assert snap.get('limit') == '50'
        assert snap.get('provider_org_guid') == org_guid

    def test_feed_failure_does_not_write_audit(self, client, db):
        """A 4xx/5xx from upstream means the read didn't happen —
        nothing to record (matches the @audit_read decorator
        convention from #227 over in request.pdhc)."""
        org_guid = str(uuid.uuid4())
        with patch(
            'app.api.auth.PATValidationService.validate'
        ) as auth_mock, patch(
            'app.api.provider.FeedService.get_feed',
            return_value=({'error': 'upstream down'}, 502),
        ):
            from app.services.pat_validation import PATValidationResult
            auth_mock.return_value = PATValidationResult(
                valid=True, provider_org_guid=org_guid,
                scopes='read', error=None,
            )
            r = client.get(
                '/api/v1/provider/feed',
                headers={'X-Provider-Token': 'pat-token'},
            )
        assert r.status_code == 502
        assert AuditLog.query.filter_by(
            event_type='provider.feed.polled',
        ).count() == 0
