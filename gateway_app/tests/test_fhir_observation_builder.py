"""#489 FHIR value-typing + #490 forwarder provenance / crash."""
from datetime import datetime, timezone
from types import SimpleNamespace

import app.services.cdr_forwarder as fwd
from app.services.fhir_observation_builder import build_fhir_observation
from app.services.sr_context import SRContextResult


def _row(fhir_json, **over):
    d = dict(guid="r1", concept_guid="c1", patient_guid="p1", service_request_guid=None,
             contract_guid=None, provider_org_guid=None, transaction_guid=None,
             received_at=datetime(2026, 8, 1, tzinfo=timezone.utc), fhir_observation_json=fhir_json)
    d.update(over)
    return SimpleNamespace(**d)


# ---- #489 value typing --------------------------------------------------
def test_boolean_is_valueBoolean_not_string():
    obs = build_fhir_observation(_row({"value": True, "response_type": "boolean"}))
    assert obs["valueBoolean"] is True
    assert "valueString" not in obs and "valueQuantity" not in obs


def test_categorical_is_codeableconcept_not_quantity():
    obs = build_fhir_observation(_row({"value": "120", "response_type": "categorical"}))
    assert "valueQuantity" not in obs  # the core corruption bug
    assert obs["valueCodeableConcept"]["text"] == "120"
    assert obs["valueCodeableConcept"]["coding"][0]["code"] == "120"


def test_numeric_unit_display_vs_machine_code():
    obs = build_fhir_observation(_row(
        {"value": 5.4, "response_type": "numeric", "unit_display": "mmol/L", "unit": "mmol-per-L"}))
    vq = obs["valueQuantity"]
    assert vq["value"] == 5.4
    assert vq["unit"] == "mmol/L"       # human display
    assert vq["code"] == "mmol-per-L"   # machine code (was wrongly the display)


def test_datetime_and_text_types():
    o1 = build_fhir_observation(_row({"value": "2026-08-01T10:00:00Z", "response_type": "dateTime"}))
    assert o1["valueDateTime"] == "2026-08-01T10:00:00Z"
    o2 = build_fhir_observation(_row({"value": "free text note", "response_type": "text"}))
    assert o2["valueString"] == "free text note"


def test_category_survey_vs_laboratory():
    survey = build_fhir_observation(_row({"value": True, "response_type": "boolean"}))
    assert survey["category"][0]["coding"][0]["code"] == "survey"
    lab = build_fhir_observation(_row({"value": 5, "response_type": "numeric"}))
    assert lab["category"][0]["coding"][0]["code"] == "laboratory"


def test_meta_profile_declared():
    obs = build_fhir_observation(_row({"value": 5, "response_type": "numeric"}))
    assert obs["meta"]["profile"]


# ---- #490 crash + provenance -------------------------------------------
def test_missing_value_key_does_not_crash():
    # was: value = row.value -> AttributeError (CdrDeliveryLog has no such column)
    obs = build_fhir_observation(_row({"response_type": "numeric"}))  # no 'value' key
    assert "valueQuantity" not in obs and "valueString" not in obs  # missing data, no crash


def test_forwarder_restores_provenance(monkeypatch):
    log = SimpleNamespace(
        guid="l1", concept_guid="c1", patient_guid="p1", service_request_guid="sr1",
        contract_guid="k1", provider_org_guid="o1", transaction_guid="t1",
        received_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        fhir_observation_json={"value": 5.4, "response_type": "numeric"})
    fake = SRContextResult(found=True, data={
        "plan_definition_guid": "pd1", "requesting_org_guid": "ro1", "goals": []})
    monkeypatch.setattr(fwd.SRContextService, "fetch", staticmethod(lambda g: fake))
    obs = fwd._build_payload(log)["fhir_resource"]
    assert any(b.get("type") == "PlanDefinition" and "pd1" in b["reference"]
               for b in obs.get("basedOn", []))
    assert any(e["url"].endswith("requesting-organization") for e in obs.get("extension", []))


def test_forwarder_survives_context_fetch_failure(monkeypatch):
    log = SimpleNamespace(
        guid="l2", concept_guid="c1", patient_guid="p1", service_request_guid="sr1",
        contract_guid=None, provider_org_guid=None, transaction_guid=None,
        received_at=None, fhir_observation_json={"value": 5, "response_type": "numeric"})

    def boom(g):
        raise RuntimeError("request.pdhc down")
    monkeypatch.setattr(fwd.SRContextService, "fetch", staticmethod(boom))
    obs = fwd._build_payload(log)["fhir_resource"]  # must not raise
    assert obs["valueQuantity"]["value"] == 5
