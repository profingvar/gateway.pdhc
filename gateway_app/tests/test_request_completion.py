"""Tests for tillägg 7 — request completion tracking.

Tests:
1. Track delivery creates new status record
2. Track delivery updates existing record
3. Completion detected when all transactions delivered
4. Expiry detected when grant expires with no deliveries
5. Partial detected when grant expires with some deliveries
6. Check expirations batch processor
7. Set expected transactions
8. Delivery progress property
9. Web page loads
10. Web page shows status records
11. Web page filters by status
12. Report submission triggers tracking
"""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from app.models import ServiceRequestStatus, InboundObservation
from app.services.request_completion import RequestCompletionService


class TestRequestCompletion:

    def test_track_creates_new_record(self, db):
        srs = RequestCompletionService.track_delivery(
            'sr-001', 'pat-001', 'org-001', 'con-001',
            observations_count=3,
        )
        assert srs is not None
        assert srs.service_request_guid == 'sr-001'
        assert srs.status == 'active'
        assert srs.total_observations == 3

    def test_track_updates_existing(self, db):
        RequestCompletionService.track_delivery(
            'sr-002', 'pat-002', 'org-002', 'con-002',
            observations_count=2,
        )
        srs = RequestCompletionService.track_delivery(
            'sr-002', 'pat-002', 'org-002', 'con-002',
            observations_count=3,
        )
        assert srs.total_observations == 5

    def test_completed_when_all_delivered(self, db):
        # Create observations with distinct transaction GUIDs
        for txn in ['txn-a', 'txn-b', 'txn-c']:
            obs = InboundObservation(
                service_request_guid='sr-003',
                transaction_guid=txn,
                patient_guid='pat-003',
                provider_org_guid='org-003',
                contract_guid='con-003',
                fhir_observation_json={'value': 1},
                validation_status='valid',
                resolution_status='pending',
            )
            db.session.add(obs)
        db.session.commit()

        srs = RequestCompletionService.track_delivery(
            'sr-003', 'pat-003', 'org-003', 'con-003',
            observations_count=3,
            transaction_guids=['txn-a', 'txn-b', 'txn-c'],
        )
        # Set expected = 3
        RequestCompletionService.set_expected_transactions('sr-003', 3)

        srs = ServiceRequestStatus.query.filter_by(
            service_request_guid='sr-003').first()
        assert srs.status == 'completed'
        assert srs.completed_at is not None

    def test_expired_no_deliveries(self, db):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        srs = RequestCompletionService.track_delivery(
            'sr-004', 'pat-004', 'org-004', 'con-004',
            observations_count=0,
            expires_at_iso=past.isoformat(),
        )
        assert srs.status == 'expired'

    def test_partial_when_expired_with_deliveries(self, db):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        # Create an observation first
        obs = InboundObservation(
            service_request_guid='sr-005',
            transaction_guid='txn-x',
            patient_guid='pat-005',
            provider_org_guid='org-005',
            contract_guid='con-005',
            fhir_observation_json={'value': 1},
            validation_status='valid',
            resolution_status='pending',
        )
        db.session.add(obs)
        db.session.commit()

        srs = RequestCompletionService.track_delivery(
            'sr-005', 'pat-005', 'org-005', 'con-005',
            observations_count=1,
            expires_at_iso=past.isoformat(),
            transaction_guids=['txn-x'],
        )
        assert srs.status == 'partial'

    def test_check_expirations_batch(self, db):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        # Create an active record with past expiry
        srs = ServiceRequestStatus(
            service_request_guid='sr-006',
            patient_guid='pat-006',
            provider_org_guid='org-006',
            contract_guid='con-006',
            status='active',
            grant_expires_at=past,
        )
        db.session.add(srs)
        db.session.commit()

        count = RequestCompletionService.check_expirations()
        assert count == 1

        srs = ServiceRequestStatus.query.filter_by(
            service_request_guid='sr-006').first()
        assert srs.status == 'expired'

    def test_set_expected_transactions(self, db):
        RequestCompletionService.track_delivery(
            'sr-007', 'pat-007', 'org-007', 'con-007',
            observations_count=1,
        )
        srs = RequestCompletionService.set_expected_transactions('sr-007', 5)
        assert srs.expected_transactions == 5

    def test_delivery_progress_property(self, db):
        srs = RequestCompletionService.track_delivery(
            'sr-008', 'pat-008', 'org-008', 'con-008',
            observations_count=2,
        )
        assert srs.delivery_progress == '0/?'

        srs.expected_transactions = 5
        db.session.commit()
        assert srs.delivery_progress == '0/5'


class TestRequestsPage:

    def test_page_loads(self, client):
        resp = client.get('/requests')
        assert resp.status_code == 200
        assert b'Service Request Status' in resp.data

    def test_page_shows_records(self, client, db):
        srs = ServiceRequestStatus(
            service_request_guid='sr-page-001',
            patient_guid='pat-page-001',
            provider_org_guid='org-page-001',
            contract_guid='con-page-001',
            status='active',
            total_observations=5,
        )
        db.session.add(srs)
        db.session.commit()

        resp = client.get('/requests')
        assert resp.status_code == 200
        assert b'sr-page-001' in resp.data

    def test_filter_by_status(self, client, db):
        for i, status in enumerate(['active', 'completed', 'expired']):
            srs = ServiceRequestStatus(
                service_request_guid=f'sr-filter-{i}',
                patient_guid=f'pat-filter-{i}',
                provider_org_guid=f'org-filter-{i}',
                contract_guid=f'con-filter-{i}',
                status=status,
            )
            db.session.add(srs)
        db.session.commit()

        resp = client.get('/requests?status=completed')
        assert resp.status_code == 200
        assert b'sr-filter-1' in resp.data
        assert b'sr-filter-0' not in resp.data


def _mock_pat_ok(*args, **kwargs):
    class Resp:
        status_code = 200
        def json(self):
            return {
                'provider_org_guid': 'org-track-001',
                'contract_guid': 'con-track-001',
                'scopes': 'read,write',
                'delivery_mode': 'poll',
            }
    return Resp()


class TestReportTriggersTracking:

    def test_submission_creates_status_record(self, client, db):
        """Report submission should create a ServiceRequestStatus record."""
        from unittest.mock import MagicMock

        class FakeResp:
            def __init__(self, code, data):
                self.status_code = code
                self._data = data
            def json(self):
                return self._data

        def _route_post(url, **kw):
            if 'validate-token' in url:
                return _mock_pat_ok(url, **kw)
            return FakeResp(200, {
                'valid': True, 'contract_guid': 'con-track-001',
                'grant_type': 'standard', 'uses_remaining': None,
            })

        def _route_get(url, **kw):
            if '/context' in url:
                return FakeResp(200, {
                    'service_request_guid': 'sr-track-001',
                    'status': 'active',
                    'patient_guid': 'pat-track-001',
                    'contract_guid': 'con-track-001',
                    'transactions': [{
                        'transaction_guid': 'txn-track-001',
                        'concept_guid': 'concept-001',
                        'concept_name': 'Test',
                        'unit': '', 'unit_display': '',
                        'range_min': None, 'range_max': None,
                        'requirement_type': 'required',
                    }],
                    'goals': [],
                })
            return FakeResp(200, {
                'contract_guid': 'con-track-001', 'status': 'active',
                'scope_defined': False, 'request_scope': [], 'return_scope': {},
            })

        with patch('app.services.pat_validation.http_requests.post', side_effect=_route_post), \
             patch('app.services.grant_validation.http_requests.post', side_effect=_route_post), \
             patch('app.services.sr_context.http_requests.get', side_effect=_route_get), \
             patch('app.services.contract_scope.http_requests.get', side_effect=_route_get):
            resp = client.post(
                '/api/v1/provider/report/sr-track-001',
                json={
                    'patient_guid': 'pat-track-001',
                    'grant_token': 'valid-grant',
                    'report_payload': {
                        'observations': [{
                            'transaction_guid': 'txn-track-001',
                            'concept_guid': 'concept-001',
                            'value': 42,
                            'response_type': 'numeric',
                        }],
                    },
                },
                headers={'X-Provider-Token': 'test-token'},
            )
            assert resp.status_code == 202

            srs = ServiceRequestStatus.query.filter_by(
                service_request_guid='sr-track-001').first()
            assert srs is not None
            assert srs.status == 'active'
            assert srs.total_observations == 1
