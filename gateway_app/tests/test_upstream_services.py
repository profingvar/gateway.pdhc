"""Integration tests — probe live sibling services the gateway depends on.

These tests verify that upstream services are reachable and respond correctly.
They hit real endpoints on the local network, so they are skipped by default
in unit-test runs.  Run explicitly with:

    pytest -m integration tests/test_upstream_services.py

Port map (from css_instrux/sso_deploy_model.md):
    request.pdhc  — 9060
    plan.pdhc     — 9030
    provider.pdhc — 9070
"""
import socket
import pytest
import requests

pytestmark = pytest.mark.integration

BASE_REQUEST = 'http://localhost:9060'
BASE_PLAN = 'http://localhost:9030'
BASE_PROVIDER = 'http://localhost:9070'

TIMEOUT = 5  # seconds


def _port_open(port):
    """Check if a local port is accepting connections."""
    try:
        with socket.create_connection(('localhost', port), timeout=2):
            return True
    except OSError:
        return False


# ── request.pdhc (port 9060) ────────────────────────────────────────

@pytest.mark.skipif(not _port_open(9060), reason='request.pdhc not running on 9060')
class TestRequestService:
    """request.pdhc — the gateway's primary upstream for PAT validation
    and GUID chain resolution."""

    def test_health(self):
        """GET /api/health — no auth required."""
        r = requests.get(f'{BASE_REQUEST}/api/health', timeout=TIMEOUT)
        assert r.status_code == 200
        data = r.json()
        assert data.get('status') in ('ok', 'healthy', True)

    def test_feed_requires_auth(self):
        """GET /api/v1/provider/feed — must return 401 without PAT."""
        r = requests.get(f'{BASE_REQUEST}/api/v1/provider/feed', timeout=TIMEOUT)
        assert r.status_code == 401

    def test_report_requires_auth(self):
        """POST /api/v1/provider/report/<guid> — must return 401 without PAT."""
        r = requests.post(
            f'{BASE_REQUEST}/api/v1/provider/report/probe-test-guid',
            json={},
            timeout=TIMEOUT,
        )
        assert r.status_code == 401


# ── plan.pdhc (port 9030) ───────────────────────────────────────────

@pytest.mark.skipif(not _port_open(9030), reason='plan.pdhc not running on 9030')
class TestPlanService:
    """plan.pdhc — PlanDefinition repository.  Gateway resolves
    plandef_guid through request.pdhc, which proxies to plan."""

    def test_health(self):
        """GET /api/health — no auth required."""
        r = requests.get(f'{BASE_PLAN}/api/health', timeout=TIMEOUT)
        assert r.status_code == 200
        data = r.json()
        assert data.get('status') in ('ok', 'healthy', True)


# ── provider.pdhc (port 9070) ───────────────────────────────────────

@pytest.mark.skipif(not _port_open(9070), reason='provider.pdhc not running on 9070')
class TestProviderService:
    """provider.pdhc — the provider portal.  Gateway will send receipts
    here once the receipt protocol is implemented."""

    def test_health(self):
        """GET /api/health — no auth required."""
        r = requests.get(f'{BASE_PROVIDER}/api/health', timeout=TIMEOUT)
        assert r.status_code == 200
        data = r.json()
        assert data.get('status') in ('ok', 'healthy', True)
