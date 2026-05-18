"""Phase 1 tests — health endpoint and scaffold verification."""


def test_health_endpoint(client):
    resp = client.get('/api/v1/health')
    assert resp.status_code in (200, 503)
    data = resp.get_json()
    assert data['service'] == 'gateway.pdhc'
    assert 'database' in data


def test_dashboard_loads(client):
    resp = client.get('/')
    assert resp.status_code == 200
    assert b'Gateway Dashboard' in resp.data


def test_report_requires_auth(client):
    resp = client.post('/api/v1/provider/report/test-guid')
    assert resp.status_code == 401


def test_feed_requires_auth(client):
    resp = client.get('/api/v1/provider/feed')
    assert resp.status_code == 401


def test_download_requires_auth(client):
    resp = client.get('/api/v1/provider/download/test-guid')
    assert resp.status_code == 401


def test_receipt_requires_auth(client):
    resp = client.post('/api/v1/provider/receipt/test-token/ack')
    assert resp.status_code == 401


def test_404_api(client):
    resp = client.get('/api/v1/nonexistent')
    assert resp.status_code == 404
    data = resp.get_json()
    assert data['code'] == 'NOT_FOUND'


def test_404_web(client):
    resp = client.get('/nonexistent-page')
    assert resp.status_code == 404
