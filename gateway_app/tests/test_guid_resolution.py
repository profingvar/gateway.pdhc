"""Phase 4 tests — GUID chain resolution and vector storage."""
import pytest
from unittest.mock import patch, MagicMock
from app.services.guid_resolution import GuidResolutionService, ResolvedChain
from app.services.vector_service import VectorService
from app.models import InboundObservation, GuidResolutionCache


# ── Mock data ───────────────────────────────────────────────────────

MOCK_SERVICE_REQUEST = {
    'guid': 'sr-001',
    'status': 'active',
    'patient_guid': 'patient-111',
    'plan_definition_guid': 'plandef-222',
    'plan_definition_snapshot': {
        'title': 'Cardiovascular Monitoring Plan',
        'name': 'cardio-monitor',
        'action': [
            {
                'id': 'act-resting-hr',
                'title': 'Resting heart rate measurement',
                'description': 'Measure resting heart rate',
                'code': [
                    {
                        'coding': [
                            {
                                'system': 'http://snomed.info/sct',
                                'code': '364075005',
                                'display': 'Heart rate',
                            }
                        ]
                    }
                ],
            },
            {
                'id': 'act-bp',
                'title': 'Blood pressure measurement',
                'description': 'Measure systolic and diastolic blood pressure',
                'code': [
                    {
                        'coding': [
                            {
                                'system': 'http://loinc.org',
                                'code': '85354-9',
                                'display': 'Blood pressure panel',
                            }
                        ]
                    }
                ],
            },
        ],
    },
    'fhir_resource': {
        'contained': [
            {
                'resourceType': 'CarePlan',
                'id': 'careplan-333',
                'title': 'Cardio monitoring careplan',
                'status': 'active',
            }
        ]
    },
    'contract_guid': 'contract-444',
    'requester_user_guid': 'user-555',
    'requester_org_guid': 'org-666',
}


class FakeResponse:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json = json_data

    def json(self):
        return self._json


def _mock_sr_fetch_ok(*args, **kwargs):
    return FakeResponse(200, MOCK_SERVICE_REQUEST)


def _mock_sr_fetch_404(*args, **kwargs):
    return FakeResponse(404, {'code': 'not_found'})


def _mock_sr_fetch_unreachable(*args, **kwargs):
    import requests
    raise requests.ConnectionError('Connection refused')


# ── GUID Resolution tests ──────────────────────────────────────────

class TestGuidResolution:

    def test_resolve_full_chain(self, app, db):
        """Resolve a service request and match a transaction by action ID."""
        with app.app_context():
            with patch('app.services.guid_resolution.http_requests.get', _mock_sr_fetch_ok):
                result = GuidResolutionService.resolve('sr-001', 'act-resting-hr')
                assert result.resolved is True
                assert result.patient_guid == 'patient-111'
                assert result.plan_definition_guid == 'plandef-222'
                assert result.concept_guid == '364075005'
                assert result.concept_name == 'Heart rate'
                assert result.careplan_guid == 'careplan-333'
                assert result.plandef_title == 'Cardiovascular Monitoring Plan'
                assert result.activity_description == 'Resting heart rate measurement'

    def test_resolve_second_action(self, app, db):
        """Match the second action in the snapshot."""
        with app.app_context():
            with patch('app.services.guid_resolution.http_requests.get', _mock_sr_fetch_ok):
                result = GuidResolutionService.resolve('sr-001', 'act-bp')
                assert result.resolved is True
                assert result.concept_guid == '85354-9'
                assert result.concept_name == 'Blood pressure panel'

    def test_resolve_without_transaction(self, app, db):
        """Resolve SR without specifying a transaction — still gets SR-level data."""
        with app.app_context():
            with patch('app.services.guid_resolution.http_requests.get', _mock_sr_fetch_ok):
                result = GuidResolutionService.resolve('sr-001')
                assert result.resolved is True
                assert result.patient_guid == 'patient-111'
                assert result.concept_guid == ''  # no transaction matched

    def test_resolve_unknown_transaction(self, app, db):
        """Transaction GUID not found in snapshot — SR resolves but no concept match."""
        with app.app_context():
            with patch('app.services.guid_resolution.http_requests.get', _mock_sr_fetch_ok):
                result = GuidResolutionService.resolve('sr-001', 'nonexistent-txn')
                assert result.resolved is True
                assert result.concept_guid == ''

    def test_resolve_upstream_404(self, app, db):
        """ServiceRequest not found on request.pdhc."""
        with app.app_context():
            with patch('app.services.guid_resolution.http_requests.get', _mock_sr_fetch_404):
                result = GuidResolutionService.resolve('sr-missing')
                assert result.resolved is False
                assert 'not found' in result.error.lower()

    def test_resolve_upstream_unreachable(self, app, db):
        """request.pdhc unreachable — resolution fails gracefully."""
        with app.app_context():
            with patch('app.services.guid_resolution.http_requests.get', _mock_sr_fetch_unreachable):
                result = GuidResolutionService.resolve('sr-001')
                assert result.resolved is False
                assert 'unreachable' in result.error.lower()

    def test_cache_hit(self, app, db):
        """Second resolve should use cache, not call upstream."""
        with app.app_context():
            with patch('app.services.guid_resolution.http_requests.get', _mock_sr_fetch_ok) as mock_get:
                # First call — hits upstream
                r1 = GuidResolutionService.resolve('sr-cache-test', 'act-resting-hr')
                assert r1.resolved is True

                # Second call — should use cache
                r2 = GuidResolutionService.resolve('sr-cache-test', 'act-resting-hr')
                assert r2.resolved is True
                assert r2.concept_guid == '364075005'

    def test_cache_stored(self, app, db):
        """After resolve, cache record should exist in DB."""
        with app.app_context():
            with patch('app.services.guid_resolution.http_requests.get', _mock_sr_fetch_ok):
                GuidResolutionService.resolve('sr-cache-check')
                cached = GuidResolutionCache.query.filter_by(
                    source_guid='sr-cache-check',
                    source_type='service_request',
                ).first()
                assert cached is not None
                assert cached.resolved_json['patient_guid'] == 'patient-111'

    def test_context_dict(self, app, db):
        """ResolvedChain.to_context_dict() returns expected structure."""
        with app.app_context():
            with patch('app.services.guid_resolution.http_requests.get', _mock_sr_fetch_ok):
                result = GuidResolutionService.resolve('sr-001', 'act-resting-hr')
                ctx = result.to_context_dict()
                assert ctx['concept_name'] == 'Heart rate'
                assert ctx['careplan_guid'] == 'careplan-333'
                assert ctx['plan_definition_guid'] == 'plandef-222'


# ── Vector storage tests ───────────────────────────────────────────

class TestVectorStorage:

    def _create_observation(self, db):
        """Helper: create a test InboundObservation."""
        obs = InboundObservation(
            service_request_guid='sr-001',
            transaction_guid='act-resting-hr',
            concept_guid='364075005',
            patient_guid='patient-111',
            provider_org_guid='org-666',
            contract_guid='contract-444',
            grant_token='test-grant',
            fhir_observation_json={'value': 72},
            value='72',
            response_type='numeric',
            payload_hash='testhash123',
            validation_status='valid',
            resolution_status='pending',
        )
        db.session.add(obs)
        db.session.commit()
        return obs

    def test_build_and_store(self, app, db):
        """Build a vector from a resolved observation."""
        with app.app_context():
            obs = self._create_observation(db)
            with patch('app.services.guid_resolution.http_requests.get', _mock_sr_fetch_ok):
                vector = VectorService.build_and_store(obs)
                assert vector is not None
                assert vector.observation_guid == obs.guid
                assert vector.careplan_guid == 'careplan-333'
                assert vector.plandef_guid == 'plandef-222'
                assert vector.transaction_guid == 'act-resting-hr'
                assert vector.resolved_context_json['concept_name'] == 'Heart rate'
                assert vector.embedding_json is not None
                assert len(vector.embedding_json) == 384
                assert obs.resolution_status == 'vectorized'

    def test_build_idempotent(self, app, db):
        """Building the same observation twice returns the existing vector."""
        with app.app_context():
            obs = self._create_observation(db)
            with patch('app.services.guid_resolution.http_requests.get', _mock_sr_fetch_ok):
                v1 = VectorService.build_and_store(obs)
                v2 = VectorService.build_and_store(obs)
                assert v1.guid == v2.guid

    def test_build_batch(self, app, db):
        """Build vectors for all pending observations under a SR."""
        with app.app_context():
            # Create two observations
            for txn in ['act-resting-hr', 'act-bp']:
                obs = InboundObservation(
                    service_request_guid='sr-batch',
                    transaction_guid=txn,
                    patient_guid='patient-111',
                    provider_org_guid='org-666',
                    contract_guid='contract-444',
                    grant_token='test-grant',
                    fhir_observation_json={'value': 72},
                    value='72',
                    response_type='numeric',
                    payload_hash=f'hash-{txn}',
                    validation_status='valid',
                    resolution_status='pending',
                )
                db.session.add(obs)
            db.session.commit()

            with patch('app.services.guid_resolution.http_requests.get', _mock_sr_fetch_ok):
                result = VectorService.build_batch('sr-batch')
                assert result['total'] == 2
                assert result['vectorized'] == 2
                assert result['failed'] == 0

    def test_build_fails_when_upstream_down(self, app, db):
        """Vector build fails gracefully when upstream is unreachable."""
        with app.app_context():
            obs = self._create_observation(db)
            with patch('app.services.guid_resolution.http_requests.get', _mock_sr_fetch_unreachable):
                vector = VectorService.build_and_store(obs)
                assert vector is None
                assert obs.resolution_status == 'failed'

    def test_query_by_patient(self, app, db):
        """Query vectors by patient GUID."""
        with app.app_context():
            obs = self._create_observation(db)
            with patch('app.services.guid_resolution.http_requests.get', _mock_sr_fetch_ok):
                VectorService.build_and_store(obs)
                vectors = VectorService.query_by_patient('patient-111')
                assert len(vectors) == 1
                assert vectors[0]['careplan_guid'] == 'careplan-333'

    def test_query_by_careplan(self, app, db):
        """Query vectors by careplan GUID."""
        with app.app_context():
            obs = self._create_observation(db)
            with patch('app.services.guid_resolution.http_requests.get', _mock_sr_fetch_ok):
                VectorService.build_and_store(obs)
                vectors = VectorService.query_by_careplan('careplan-333')
                assert len(vectors) == 1

    def test_query_empty(self, app, db):
        """Query returns empty list when no vectors exist."""
        with app.app_context():
            vectors = VectorService.query_by_patient('nonexistent')
            assert vectors == []


# ── Vector endpoint tests ──────────────────────────────────────────

class TestVectorEndpoints:

    def test_vectors_by_patient(self, client, db):
        resp = client.get('/api/v1/vectors/by-patient/patient-111')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'vectors' in data
        assert 'count' in data

    def test_vectors_by_careplan(self, client, db):
        resp = client.get('/api/v1/vectors/by-careplan/careplan-333')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'vectors' in data

    def test_vectors_similar(self, client, db):
        resp = client.post('/api/v1/vectors/similar',
                           json={'context': {'concept_guid': '364075005'}, 'limit': 5})
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'vectors' in data

    def test_resolve_and_vectorize(self, app, client, db):
        """Trigger vectorization for a SR with pending observations."""
        with app.app_context():
            obs = InboundObservation(
                service_request_guid='sr-endpoint-test',
                transaction_guid='act-resting-hr',
                patient_guid='patient-111',
                provider_org_guid='org-666',
                contract_guid='contract-444',
                grant_token='test-grant',
                fhir_observation_json={'value': 72},
                value='72',
                response_type='numeric',
                payload_hash='hash-endpoint',
                validation_status='valid',
                resolution_status='pending',
            )
            db.session.add(obs)
            db.session.commit()

            with patch('app.services.guid_resolution.http_requests.get', _mock_sr_fetch_ok):
                resp = client.post('/api/v1/vectors/resolve/sr-endpoint-test')
                assert resp.status_code == 200
                data = resp.get_json()
                assert data['vectorized'] == 1
