"""Phase 6 tests — Push delivery service."""
import pytest
from unittest.mock import patch, MagicMock
from app.services.push_service import PushService, PushDeliveryResult
from app.models import AuditLog


class FakeResponse:
    def __init__(self, status_code, text=''):
        self.status_code = status_code
        self.text = text


MOCK_BUNDLE = {
    'resourceType': 'Bundle',
    'type': 'collection',
    'entry': [
        {'resource': {'resourceType': 'ServiceRequest', 'id': 'sr-001'}},
    ],
}


class TestPushDelivery:

    def test_push_success(self, app, db):
        """Successful push returns success with correlation and receipt IDs."""
        with app.app_context():
            with patch('app.services.push_service.http_requests.post') as mock_post:
                mock_post.return_value = FakeResponse(202)
                result = PushService.push_to_provider(
                    'http://provider.local/api/v1/push',
                    MOCK_BUNDLE,
                    'push-secret-123',
                    patient_guid='patient-111',
                    provider_org_guid='org-222',
                )
                assert result.success is True
                assert result.status_code == 202
                assert result.correlation_id is not None
                assert result.receipt_guid is not None

    def test_push_mutual_auth_header(self, app, db):
        """Push sends X-Push-Secret and X-Correlation-Id headers."""
        with app.app_context():
            with patch('app.services.push_service.http_requests.post') as mock_post:
                mock_post.return_value = FakeResponse(200)
                PushService.push_to_provider(
                    'http://provider.local/push',
                    MOCK_BUNDLE,
                    'secret-abc',
                )
                call_kwargs = mock_post.call_args
                headers = call_kwargs.kwargs.get('headers', {})
                assert headers['X-Push-Secret'] == 'secret-abc'
                assert 'X-Correlation-Id' in headers
                assert 'X-Receipt-Guid' in headers

    def test_push_retry_on_failure(self, app, db):
        """Push retries up to PUSH_RETRY_COUNT times."""
        with app.app_context():
            app.config['PUSH_RETRY_COUNT'] = 3
            with patch('app.services.push_service.http_requests.post') as mock_post:
                mock_post.return_value = FakeResponse(500, 'Internal Server Error')
                result = PushService.push_to_provider(
                    'http://provider.local/push',
                    MOCK_BUNDLE,
                    'secret',
                )
                assert result.success is False
                assert mock_post.call_count == 3
                assert 'HTTP 500' in result.error

    def test_push_retry_then_succeed(self, app, db):
        """Push succeeds on second attempt after initial failure."""
        with app.app_context():
            with patch('app.services.push_service.http_requests.post') as mock_post:
                mock_post.side_effect = [
                    FakeResponse(500, 'Error'),
                    FakeResponse(202),
                ]
                result = PushService.push_to_provider(
                    'http://provider.local/push',
                    MOCK_BUNDLE,
                    'secret',
                )
                assert result.success is True
                assert mock_post.call_count == 2

    def test_push_connection_refused(self, app, db):
        """Push handles connection refused gracefully."""
        with app.app_context():
            app.config['PUSH_RETRY_COUNT'] = 1
            with patch('app.services.push_service.http_requests.post') as mock_post:
                import requests
                mock_post.side_effect = requests.ConnectionError('refused')
                result = PushService.push_to_provider(
                    'http://provider.local/push',
                    MOCK_BUNDLE,
                    'secret',
                )
                assert result.success is False
                assert 'Connection refused' in result.error

    def test_push_timeout(self, app, db):
        """Push handles timeout gracefully."""
        with app.app_context():
            app.config['PUSH_RETRY_COUNT'] = 1
            with patch('app.services.push_service.http_requests.post') as mock_post:
                import requests
                mock_post.side_effect = requests.Timeout()
                result = PushService.push_to_provider(
                    'http://provider.local/push',
                    MOCK_BUNDLE,
                    'secret',
                )
                assert result.success is False
                assert 'Timeout' in result.error

    def test_push_audited_on_success(self, app, db):
        """Successful push creates audit entry."""
        with app.app_context():
            with patch('app.services.push_service.http_requests.post') as mock_post:
                mock_post.return_value = FakeResponse(202)
                PushService.push_to_provider(
                    'http://provider.local/push',
                    MOCK_BUNDLE,
                    'secret',
                    patient_guid='patient-111',
                    provider_org_guid='org-222',
                )
            audits = AuditLog.query.filter_by(event_type='bundle.pushed').all()
            assert len(audits) == 1
            assert audits[0].data_subject_guid == 'patient-111'

    def test_push_audited_on_failure(self, app, db):
        """Failed push creates audit entry."""
        with app.app_context():
            app.config['PUSH_RETRY_COUNT'] = 1
            with patch('app.services.push_service.http_requests.post') as mock_post:
                mock_post.return_value = FakeResponse(500, 'error')
                PushService.push_to_provider(
                    'http://provider.local/push',
                    MOCK_BUNDLE,
                    'secret',
                    provider_org_guid='org-222',
                )
            audits = AuditLog.query.filter_by(event_type='bundle.push_failed').all()
            assert len(audits) == 1

    def test_push_result_to_dict(self, app, db):
        """PushDeliveryResult serializes correctly."""
        r = PushDeliveryResult(
            success=True, status_code=202,
            correlation_id='corr-1', receipt_guid='rcpt-1',
        )
        d = r.to_dict()
        assert d['success'] is True
        assert d['correlation_id'] == 'corr-1'


class TestReceiptPush:

    def test_receipt_delivery_success(self, app, db):
        """Receipt push to provider returns True on success."""
        with app.app_context():
            with patch('app.services.push_service.http_requests.post') as mock_post:
                mock_post.return_value = FakeResponse(201)
                ok = PushService.send_receipt_to_provider(
                    'http://localhost:9070/api/v1',
                    {'receipt_guid': 'rcpt-1', 'service_request_guid': 'sr-1'},
                )
                assert ok is True
                call_kwargs = mock_post.call_args
                assert '/receipts/ingest' in call_kwargs.args[0]

    def test_receipt_delivery_failure(self, app, db):
        """Receipt push returns False on upstream error."""
        with app.app_context():
            with patch('app.services.push_service.http_requests.post') as mock_post:
                mock_post.return_value = FakeResponse(500)
                ok = PushService.send_receipt_to_provider(
                    'http://localhost:9070/api/v1',
                    {'receipt_guid': 'rcpt-1'},
                )
                assert ok is False

    def test_receipt_delivery_connection_error(self, app, db):
        """Receipt push returns False when provider unreachable."""
        with app.app_context():
            with patch('app.services.push_service.http_requests.post') as mock_post:
                import requests
                mock_post.side_effect = requests.ConnectionError()
                ok = PushService.send_receipt_to_provider(
                    'http://localhost:9070/api/v1',
                    {'receipt_guid': 'rcpt-1'},
                )
                assert ok is False
