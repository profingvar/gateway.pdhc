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


# ---- #500 canonical observation block -----------------------------------
def test_canonical_carries_typed_value():
    from app.services.fhir_observation_builder import build_canonical_observation
    co = build_canonical_observation(_row({"value": True, "response_type": "boolean", "concept_guid": "c1"}))
    assert co["value"] is True and co["response_type"] == "boolean"


def test_fhir_carries_canonical_extension():
    import json
    from app.services.fhir_observation_builder import CANONICAL_OBS_EXT
    obs = build_fhir_observation(_row({"value": "120", "response_type": "categorical"}))
    ext = next(e for e in obs["extension"] if e["url"] == CANONICAL_OBS_EXT)
    payload = json.loads(ext["valueString"])
    assert payload["value"] == "120" and payload["response_type"] == "categorical"


def test_forwarder_envelope_has_observation_block():
    log = SimpleNamespace(
        guid="l", concept_guid="c1", patient_guid="p", service_request_guid=None,
        contract_guid=None, provider_org_guid=None, transaction_guid=None, received_at=None,
        fhir_observation_json={"value": 72.5, "response_type": "numeric", "unit": "kg"})
    payload = fwd._build_payload(log)
    assert payload["observation"]["value"] == 72.5
    assert payload["observation"]["response_type"] == "numeric"
    assert "fhir_resource" in payload  # projection kept for back-compat


# ---- #776: the clinical_context block the CDR actually stores -------------
#
# #490 made the forwarder FETCH the SR context and hand it to the FHIR builder,
# which is why `test_forwarder_restores_provenance` above passes. But the
# `clinical_context` dict — the block cdr reads to populate its ClinicalContext
# row — never carried those fields, so cdr1 recorded plan_definition_guid and
# requesting_org_guid on 0 of 7,416 rows while the values sat in the fetched
# response. Asserting on the FHIR projection alone did not catch it; these
# assert on the block cdr stores.

def _log(**kw):
    base = dict(
        guid="l776", concept_guid="c1", patient_guid="p1",
        service_request_guid="sr1", contract_guid="k1", provider_org_guid="o1",
        transaction_guid="t1", received_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        fhir_observation_json={"value": 5.4, "response_type": "numeric"})
    base.update(kw)
    return SimpleNamespace(**base)


def _ctx(**kw):
    data = {"plan_definition_guid": "pd1", "requesting_org_guid": "ro1",
            "requester_user_guid": "ru1", "goals": []}
    data.update(kw)
    return SRContextResult(found=True, data=data)


def test_clinical_context_block_carries_the_fetched_provenance(monkeypatch):
    monkeypatch.setattr(fwd.SRContextService, "fetch", staticmethod(lambda g: _ctx()))
    cc = fwd._build_payload(_log())["clinical_context"]
    assert cc["plan_definition_guid"] == "pd1"
    assert cc["requesting_org_guid"] == "ro1"
    assert cc["requester_user_guid"] == "ru1"


def test_the_existing_clinical_context_fields_are_unchanged(monkeypatch):
    """Additive only — the keys cdr already relies on must keep their values,
    so gateway and cdr can be deployed in either order."""
    monkeypatch.setattr(fwd.SRContextService, "fetch", staticmethod(lambda g: _ctx()))
    cc = fwd._build_payload(_log())["clinical_context"]
    assert cc["service_request_guid"] == "sr1"
    assert cc["transaction_guid"] == "t1"
    assert cc["contract_guid"] == "k1"
    assert cc["provider_org_guid"] == "o1"


def test_a_context_outage_still_delivers_the_observation(monkeypatch):
    """THE constraint: request.pdhc being down must degrade provenance, never
    block clinical data reaching the CDR."""
    def boom(g):
        raise RuntimeError("request.pdhc down")
    monkeypatch.setattr(fwd.SRContextService, "fetch", staticmethod(boom))
    payload = fwd._build_payload(_log())          # must not raise
    assert payload["fhir_resource"]["valueQuantity"]["value"] == 5.4
    cc = payload["clinical_context"]
    assert cc["plan_definition_guid"] is None     # absent, not invented
    assert cc["requesting_org_guid"] is None
    assert cc["service_request_guid"] == "sr1"    # what we DID know survives


def test_a_missing_field_is_None_not_an_empty_string(monkeypatch):
    """cdr stores NULL rather than defaulting (#665). An empty string would be a
    value, and 'not declared' would become indistinguishable from 'declared as
    blank'."""
    monkeypatch.setattr(fwd.SRContextService, "fetch",
                        staticmethod(lambda g: _ctx(plan_definition_guid="",
                                                    requester_user_guid="")))
    cc = fwd._build_payload(_log())["clinical_context"]
    assert cc["plan_definition_guid"] is None
    assert cc["requester_user_guid"] is None
    assert cc["requesting_org_guid"] == "ro1"


def test_a_delivery_with_no_service_request_still_builds(monkeypatch):
    """The forwarder drains a queue; a row can legitimately have no SR."""
    monkeypatch.setattr(fwd.SRContextService, "fetch",
                        staticmethod(lambda g: _ctx()))
    cc = fwd._build_payload(_log(service_request_guid=None))["clinical_context"]
    assert cc["plan_definition_guid"] is None
    assert cc["service_request_guid"] is None
