"""2026-11-20 — the CarePlan reaches the CDR's clinical_context.

request.pdhc's SR context names the CarePlan the ServiceRequest is based on;
the forwarder dropped it, so 0 of 34 observations gateway delivered to cdr1
carried a care_plan_guid.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from app.services import cdr_forwarder
from app.services.sr_context import SRContextResult


def _log():
    return SimpleNamespace(
        guid="log-1", patient_guid="p-1", service_request_guid="sr-1",
        transaction_guid="tx-1", contract_guid="c-1", provider_org_guid="o-1",
        author_org_guid=None)


def _payload(ctx):
    with patch.object(cdr_forwarder.SRContextService, "fetch", return_value=ctx), \
         patch.object(cdr_forwarder, "build_fhir_observation", return_value={}), \
         patch.object(cdr_forwarder, "build_canonical_observation", return_value={}):
        return cdr_forwarder._build_payload(_log())["clinical_context"]


def test_the_requests_care_plan_is_forwarded():
    ctx = SRContextResult(found=True, data={"care_plan_guid": "cp-1",
                                            "plan_definition_guid": "pd-1"})
    cc = _payload(ctx)
    assert cc["care_plan_guid"] == "cp-1"
    assert cc["plan_definition_guid"] == "pd-1"


def test_no_care_plan_in_the_context_forwards_none():
    assert _payload(SRContextResult(found=True, data={}))["care_plan_guid"] is None


def test_an_unavailable_context_forwards_none_and_still_delivers():
    cc = _payload(SRContextResult(found=False, error="down"))
    assert cc["care_plan_guid"] is None and cc["service_request_guid"] == "sr-1"


def test_the_patients_organisation_is_forwarded():
    cc = _payload(SRContextResult(found=True, data={"patient_org_guid": "porg-1"}))
    assert cc["patient_org_guid"] == "porg-1"


def test_no_patient_org_in_the_context_forwards_none():
    assert _payload(SRContextResult(found=True, data={}))["patient_org_guid"] is None
    assert _payload(SRContextResult(found=False, error="down"))["patient_org_guid"] is None
