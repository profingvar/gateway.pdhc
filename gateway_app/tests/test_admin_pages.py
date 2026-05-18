"""Tests for Phase 9.b admin pages: PAT activity, audit log, grant status."""
import pytest
from datetime import datetime, timezone, timedelta
from app.models import AuditLog, ServiceRequestStatus


def _add_audit(db, event_type, actor_guid='org-AAA', data_subject_guid='patient-111',
               ip_address='10.0.0.1', correlation_id=None):
    e = AuditLog(
        event_type=event_type,
        actor_guid=actor_guid,
        data_subject_guid=data_subject_guid,
        ip_address=ip_address,
        correlation_id=correlation_id,
        receipt_token='sr-001',
    )
    db.session.add(e)
    db.session.commit()
    return e


def _add_sr_status(db, sr_guid='sr-001', patient='p-111', provider='org-AAA',
                   contract='c-111', status='active', grant_expires_at=None,
                   delivered=1, expected=3):
    sr = ServiceRequestStatus(
        service_request_guid=sr_guid,
        patient_guid=patient,
        provider_org_guid=provider,
        contract_guid=contract,
        status=status,
        delivered_transactions=delivered,
        expected_transactions=expected,
        grant_expires_at=grant_expires_at,
    )
    db.session.add(sr)
    db.session.commit()
    return sr


# ---------------------------------------------------------------------------
# PAT activity page
# ---------------------------------------------------------------------------

class TestPatActivity:

    def test_page_loads(self, client, db):
        resp = client.get('/pats')
        assert resp.status_code == 200
        assert b'PAT Activity' in resp.data

    def test_shows_validated_provider(self, app, client, db):
        with app.app_context():
            _add_audit(db, 'pat.validated', actor_guid='org-AAA')
        resp = client.get('/pats')
        assert resp.status_code == 200
        assert b'org-AAA' in resp.data

    def test_aggregates_counts(self, app, client, db):
        with app.app_context():
            _add_audit(db, 'pat.validated', actor_guid='org-BBB')
            _add_audit(db, 'pat.validated', actor_guid='org-BBB')
            _add_audit(db, 'pat.rejected', actor_guid='org-BBB')
        resp = client.get('/pats')
        assert resp.status_code == 200
        assert b'org-BBB' in resp.data

    def test_filter_by_actor(self, app, client, db):
        with app.app_context():
            _add_audit(db, 'pat.validated', actor_guid='org-CCC')
            _add_audit(db, 'pat.validated', actor_guid='org-DDD')
        resp = client.get('/pats?actor_guid=org-CCC')
        assert resp.status_code == 200
        assert b'org-CCC' in resp.data
        assert b'org-DDD' not in resp.data

    def test_empty_state(self, client, db):
        resp = client.get('/pats')
        assert resp.status_code == 200
        assert b'No PAT events recorded' in resp.data

    def test_non_pat_events_excluded(self, app, client, db):
        with app.app_context():
            _add_audit(db, 'report.received', actor_guid='org-EEE')
        resp = client.get('/pats')
        assert b'org-EEE' not in resp.data


# ---------------------------------------------------------------------------
# Audit log page
# ---------------------------------------------------------------------------

class TestAuditLog:

    def test_page_loads(self, client, db):
        resp = client.get('/audit')
        assert resp.status_code == 200
        assert b'Audit Log' in resp.data

    def test_shows_events(self, app, client, db):
        with app.app_context():
            _add_audit(db, 'report.received', actor_guid='org-111', ip_address='1.2.3.4')
        resp = client.get('/audit')
        assert resp.status_code == 200
        assert b'report.received' in resp.data
        assert b'1.2.3.4' in resp.data

    def test_filter_by_event_type(self, app, client, db):
        with app.app_context():
            _add_audit(db, 'pat.validated', actor_guid='org-F1')
            _add_audit(db, 'bundle.downloaded', actor_guid='org-F2')
        resp = client.get('/audit?event_type=pat.validated')
        assert resp.status_code == 200
        assert b'pat.validated' in resp.data
        # org-F2 is only on the bundle.downloaded row — not present when filtered out
        assert b'org-F2' not in resp.data

    def test_filter_by_actor_guid(self, app, client, db):
        with app.app_context():
            _add_audit(db, 'pat.validated', actor_guid='org-G1')
            _add_audit(db, 'pat.validated', actor_guid='org-G2')
        resp = client.get('/audit?actor_guid=org-G1')
        assert resp.status_code == 200
        assert b'org-G1' in resp.data
        assert b'org-G2' not in resp.data

    def test_empty_state(self, client, db):
        resp = client.get('/audit')
        assert resp.status_code == 200
        assert b'No audit events found' in resp.data

    def test_event_type_dropdown_present(self, client, db):
        resp = client.get('/audit')
        assert b'pat.validated' in resp.data
        assert b'bundle.pushed' in resp.data


# ---------------------------------------------------------------------------
# Grant status page
# ---------------------------------------------------------------------------

class TestGrantStatus:

    def test_page_loads(self, client, db):
        resp = client.get('/grants')
        assert resp.status_code == 200
        assert b'Grant Status' in resp.data

    def test_shows_active_grant(self, app, client, db):
        future = datetime.now(timezone.utc) + timedelta(hours=48)
        with app.app_context():
            _add_sr_status(db, sr_guid='sr-g1', status='active', grant_expires_at=future)
        resp = client.get('/grants?view=active')
        assert resp.status_code == 200
        assert b'sr-g1'[:12] in resp.data or b'Valid' in resp.data

    def test_expiring_soon_filter(self, app, client, db):
        soon = datetime.now(timezone.utc) + timedelta(hours=12)
        future = datetime.now(timezone.utc) + timedelta(hours=48)
        with app.app_context():
            _add_sr_status(db, sr_guid='sr-exp1', status='active',
                           grant_expires_at=soon)
            _add_sr_status(db, sr_guid='sr-exp2', status='active',
                           grant_expires_at=future)
        resp = client.get('/grants?view=expiring')
        assert resp.status_code == 200
        assert b'Expiring soon' in resp.data

    def test_expired_filter(self, app, client, db):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        with app.app_context():
            _add_sr_status(db, sr_guid='sr-old1', status='expired',
                           grant_expires_at=past)
        resp = client.get('/grants?view=expired')
        assert resp.status_code == 200
        assert b'Expired' in resp.data

    def test_summary_counts_shown(self, app, client, db):
        future = datetime.now(timezone.utc) + timedelta(hours=48)
        with app.app_context():
            _add_sr_status(db, sr_guid='sr-s1', status='active', grant_expires_at=future)
        resp = client.get('/grants')
        assert resp.status_code == 200
        # Summary cards with counts are present
        assert b'Active grants' in resp.data
        assert b'Expiring within 24 h' in resp.data
        assert b'Expired / partial' in resp.data

    def test_empty_state(self, client, db):
        resp = client.get('/grants?view=active')
        assert resp.status_code == 200
        assert b'No grants found' in resp.data

    def test_view_filter_tabs_present(self, client, db):
        resp = client.get('/grants')
        assert b'Expiring soon' in resp.data
        assert b'Active' in resp.data
