"""POST /provider/status/<sr> is forwarded to request.pdhc.

A provider only knows gateway.pdhc. Without this route a status with no
observation data (e.g. 'acknowledged') could only go to /provider/report,
which refuses it 400. Found on the cgm_provider onboarding, 2026-11-18.
"""
from unittest.mock import patch

from tests.test_feed_service import FakeResponse

HDR = {"X-Provider-Token": "valid-token"}
BODY = {"patient_guid": "p-1", "grant_token": "g", "contract_guid": "c-1",
        "provider_org_guid": "org-111", "status": "acknowledged"}


def _router(seen, status_resp):
    def post(url, *a, **kw):
        if url.endswith("/provider/validate-token"):
            return FakeResponse(200, {"provider_org_guid": "org-111",
                                      "contract_guid": "c-1",
                                      "scopes": "read,write",
                                      "delivery_mode": "poll"})
        seen["url"], seen["json"] = url, kw.get("json")
        seen["headers"] = kw.get("headers") or {}
        return status_resp
    return post


def test_status_is_forwarded_unchanged(client, db):
    seen = {}
    with patch("requests.post", _router(seen, FakeResponse(200, {"ok": True}))):
        r = client.post("/api/v1/provider/status/sr-9", json=BODY, headers=HDR)
    assert r.status_code == 200, r.get_json()
    assert seen["url"].endswith("/provider/status/sr-9")
    assert seen["json"] == BODY
    assert seen["headers"]["X-Provider-Token"] == "valid-token"


def test_upstream_refusal_is_passed_through(client, db):
    seen = {}
    with patch("requests.post", _router(seen, FakeResponse(
            403, {"code": "unauthorized"}))):
        r = client.post("/api/v1/provider/status/sr-9", json=BODY, headers=HDR)
    assert r.status_code == 403


def test_no_token_is_refused(client, db):
    r = client.post("/api/v1/provider/status/sr-9", json=BODY)
    assert r.status_code == 401
