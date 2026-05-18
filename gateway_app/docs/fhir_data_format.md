# FHIR Data Format — Gateway Reception

This document specifies the FHIR data formats accepted by gateway.pdhc and how they align with what provider.pdhc sends. Instructions for ensuring compatibility.

## Accepted FHIR resource types

### 1. FHIR Observation (guided mode)

The primary data format. Provider submits structured observations matching the CarePlan activities.

```json
POST /api/v1/provider/report/{service_request_guid}
X-Provider-Token: <PAT>

{
  "patient_guid": "uuid",
  "contract_guid": "uuid",
  "organisation_guid": "uuid",
  "grant_token": "hmac-hex",
  "status": "completed",
  "report_payload": {
    "observations": [
      {
        "transaction_guid": "uuid",
        "concept_guid": "uuid",
        "value": 120,
        "response_type": "numeric",
        "unit": "mmHg",
        "notes": "Measured at rest",
        "recorded_at": "2026-03-25T10:00:00Z"
      }
    ]
  }
}
```

**Required fields per observation:**
| Field | Type | Description |
|-------|------|-------------|
| `transaction_guid` | string (UUID) | Links to the CarePlan activity/transaction |
| `concept_guid` | string | SNOMED/LOINC code for the concept measured |
| `value` | varies | The measured value (type must match response_type) |
| `response_type` | string | One of: numeric, categorical, text, boolean, dateTime |

**Optional fields per observation:**
| Field | Type | Description |
|-------|------|-------------|
| `unit` | string | Unit of measurement (e.g., "mmHg", "bpm") |
| `notes` | string | Provider notes or context |
| `recorded_at` | string (ISO-8601) | When the measurement was taken |

**Valid response_type values and value type constraints:**
| response_type | Expected value type | Example |
|---------------|-------------------|---------|
| numeric | int or float | `72`, `36.5` |
| categorical | string (from valueset) | `"normal"`, `"elevated"` |
| text | string | `"Patient reports improvement"` |
| boolean | boolean | `true`, `false` |
| dateTime | string (ISO-8601) | `"2026-03-25"` |

### 2. Freeform report (manual mode)

When observations key is absent, the entire report_payload is stored as a single record.

```json
{
  "report_payload": {
    "summary": "All measurements within normal range",
    "completed_by": "Dr. Smith"
  }
}
```

### 3. FHIR QuestionnaireResponse (form responses)

Not currently used by provider.pdhc. If needed in the future, the gateway will accept QuestionnaireResponse resources within report_payload as freeform data until a dedicated handler is built.

### 4. Patient characteristics and metadata

Patient identity is conveyed via GUIDs, not inline patient data:
- `patient_guid` in the composite key identifies the patient
- The full patient record is resolved via the GUID chain (gateway calls request.pdhc → IPS)
- **No patient PII is sent in the report payload** (GDPR data minimization)

Provider metadata:
- `organisation_guid` identifies the provider organisation (from PAT validation)
- `contract_guid` identifies the governing contract
- `service_request_guid` identifies the clinical request

## Alignment with provider.pdhc

Provider.pdhc's `guided_response.py` builds observations from CarePlan activity responses. The gateway accepts exactly this format:

| Provider sends | Gateway expects | Status |
|---------------|-----------------|--------|
| `transaction_guid` | Required | Aligned |
| `concept_guid` | Required | Aligned |
| `value` | Required | Aligned |
| `response_type` | Required | Aligned |
| `unit` | Optional (stored in fhir_observation_json) | Aligned |
| `notes` | Optional (stored in fhir_observation_json) | Aligned |
| `recorded_at` | Optional (stored in fhir_observation_json) | Aligned |

## Instructions for provider.pdhc

Provider.pdhc's current `upstream_client.py` already submits data in the correct format. No changes needed to the submission code.

To verify alignment:
1. Ensure `report_payload.observations` array is included
2. Each observation has all 4 required fields
3. `value` type matches declared `response_type`
4. Optional `unit`, `notes`, `recorded_at` are passed through and stored
