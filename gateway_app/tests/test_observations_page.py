"""Tests for the observations list and detail pages (tilläggsuppdrag 2)."""
import pytest
from unittest.mock import patch
from app.models import InboundObservation, AuditLog
from flask import g


def _create_observation(db, patient_guid='patient-111', provider_org_guid='org-222',
                        value='72', sr_guid='sr-001', concept_guid='364075005'):
    obs = InboundObservation(
        service_request_guid=sr_guid,
        transaction_guid='txn-001',
        concept_guid=concept_guid,
        patient_guid=patient_guid,
        provider_org_guid=provider_org_guid,
        contract_guid='contract-333',
        grant_token='test-grant',
        fhir_observation_json={'value': value, 'response_type': 'numeric'},
        value=value,
        response_type='numeric',
        payload_hash=f'hash-{value}-{sr_guid}',
        validation_status='valid',
        resolution_status='pending',
    )
    db.session.add(obs)
    db.session.commit()
    return obs


class TestObservationsList:

    def test_list_page_loads(self, client, db):
        resp = client.get('/observations')
        assert resp.status_code == 200
        assert b'Inbound Observations' in resp.data

    def test_list_shows_observations(self, app, client, db):
        with app.app_context():
            _create_observation(db)
        resp = client.get('/observations')
        assert resp.status_code == 200
        assert b'patient-111' in resp.data
        assert b'org-222' in resp.data

    def test_filter_by_patient_guid(self, app, client, db):
        with app.app_context():
            _create_observation(db, patient_guid='patient-AAA', value='10', sr_guid='sr-a')
            _create_observation(db, patient_guid='patient-BBB', value='20', sr_guid='sr-b')

        resp = client.get('/observations?patient_guid=patient-AAA')
        assert resp.status_code == 200
        assert b'patient-AAA' in resp.data
        assert b'patient-BBB' not in resp.data

    def test_filter_empty_result(self, client, db):
        resp = client.get('/observations?patient_guid=nonexistent')
        assert resp.status_code == 200
        assert b'No observations found' in resp.data

    def test_receipt_badge_shown(self, app, client, db):
        """When a report.received audit exists, receipt badge should show 'Sent'."""
        with app.app_context():
            obs = _create_observation(db)
            audit = AuditLog(
                event_type='report.received',
                actor_guid='org-222',
                data_subject_guid='patient-111',
                resource_guid='sr-001',
                ip_address='127.0.0.1',
            )
            db.session.add(audit)
            db.session.commit()

        resp = client.get('/observations')
        assert b'Sent' in resp.data


class TestObservationDetail:

    def test_detail_page_loads(self, app, client, db):
        with app.app_context():
            obs = _create_observation(db)
            guid = obs.guid
        resp = client.get(f'/observations/{guid}')
        assert resp.status_code == 200
        assert b'Observation Detail' in resp.data
        assert b'patient-111' in resp.data
        assert b'org-222' in resp.data
        assert b'contract-333' in resp.data

    def test_detail_404(self, client, db):
        resp = client.get('/observations/nonexistent-guid')
        assert resp.status_code == 404

    def test_detail_shows_audit_trail(self, app, client, db):
        with app.app_context():
            obs = _create_observation(db)
            audit = AuditLog(
                event_type='report.received',
                actor_guid='org-222',
                data_subject_guid='patient-111',
                resource_guid='sr-001',
                ip_address='127.0.0.1',
            )
            db.session.add(audit)
            db.session.commit()
            guid = obs.guid

        resp = client.get(f'/observations/{guid}')
        assert b'report.received' in resp.data

    def test_detail_shows_fhir_json(self, app, client, db):
        with app.app_context():
            obs = _create_observation(db)
            guid = obs.guid
        resp = client.get(f'/observations/{guid}')
        assert b'FHIR Observation JSON' in resp.data
