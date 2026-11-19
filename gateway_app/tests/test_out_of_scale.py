"""2026-11-19: a value outside its CONCEPT'S SCALE (from request.pdhc's
transaction map, scale_min/scale_max) is dropped with OUT_OF_SCALE; the rest
of the report is accepted. The plan's clinical target range is never a reason
to refuse (MMD sent stress 8 and 9 on a 1-7 scale and both were stored)."""
from tests.test_report_submission import (_make_minimal_body, _mock_sr_context,
                                          _patch_all_upstreams)


def _ctx(scale_min, scale_max):
    def f(*a, **k):
        r = _mock_sr_context(*a, **k)
        tx = r.json.return_value['transactions'][0]      # tx-001, value 72, target 70-120
        tx['scale_min'], tx['scale_max'] = scale_min, scale_max
        return r
    return f


def _post(client, ctx, value=72):
    body = _make_minimal_body()
    body['status'] = 'in-progress'
    body['report_payload']['observations'][0]['value'] = value
    p = _patch_all_upstreams(sr_context_mock=ctx)
    with p[0], p[1], p[2], p[3]:
        return client.post('/api/v1/provider/report/sr-scale', json=body,
                           headers={'X-Provider-Token': 'valid-token'})


def test_out_of_scale_value_is_dropped_and_the_rest_stored(client, app, db):
    r = _post(client, _ctx(1, 7))
    assert r.status_code == 202, r.get_json()
    d = r.get_json()
    assert d['action'] == 'partial' and d['observations_stored'] == 1
    rej = d['observations_rejected']
    assert rej[0]['code'] == 'OUT_OF_SCALE' and rej[0]['transaction_guid'] == 'tx-001'
    assert rej[0]['observation_index'] == 0


def test_outside_the_clinical_target_but_on_the_scale_is_stored(client, app, db):
    r = _post(client, _ctx(0, 300), value=160)               # target 70-120
    assert r.status_code == 202
    assert not r.get_json().get('observations_rejected')


def test_every_value_out_of_scale_is_a_422(client, app, db):
    def both(*a, **k):
        r = _ctx(1, 7)(*a, **k)
        r.json.return_value['transactions'] = r.json.return_value['transactions'][:1]
        return r
    body = _make_minimal_body()
    body['status'] = 'in-progress'
    body['report_payload']['observations'] = body['report_payload']['observations'][:1]
    p = _patch_all_upstreams(sr_context_mock=both)
    with p[0], p[1], p[2], p[3]:
        r = client.post('/api/v1/provider/report/sr-scale', json=body,
                        headers={'X-Provider-Token': 'valid-token'})
    assert r.status_code == 422 and r.get_json()['code'] == 'OUT_OF_SCALE'
