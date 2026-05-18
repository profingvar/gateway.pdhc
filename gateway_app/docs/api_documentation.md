# Gateway.pdhc — API Documentation

Base URL: `https://gateway.pdhc.se/api/v1`

## Authentication

All provider endpoints require a Provider Access Token (PAT) in the `X-Provider-Token` header. PATs are issued by request.pdhc.

```
X-Provider-Token: <your-pat-here>
```

### Scopes

| Scope | Grants access to |
|-------|------------------|
| `read` | Feed, download endpoints |
| `write` | Report submission, receipt acknowledgement |

---

## Endpoints

### Health Check

```
GET /api/v1/health
```

No authentication required.

**Response 200:**
```json
{ "status": "ok" }
```

---

### Submit Observation Report

```
POST /api/v1/provider/report/{service_request_guid}
```

**Auth**: PAT with `write` scope

**Headers:**
```
X-Provider-Token: <pat>
Content-Type: application/json
X-Correlation-Id: <optional-trace-id>
```

**Request body:**
```json
{
  "patient_guid": "uuid",
  "contract_guid": "uuid",
  "organisation_guid": "uuid",
  "grant_token": "hmac-hex-digest",
  "status": "completed",
  "report_payload": {
    "observations": [
      {
        "transaction_guid": "uuid",
        "concept_guid": "uuid",
        "value": 72,
        "response_type": "numeric",
        "unit": "bpm",
        "notes": "Resting heart rate",
        "recorded_at": "2026-03-25T10:00:00Z"
      }
    ]
  }
}
```

**Response 202 Accepted:**
```json
{
  "status": "accepted",
  "receipt_guid": "uuid",
  "service_request_guid": "uuid",
  "observations_stored": 1,
  "action": "created"
}
```

**Error responses:**
| Status | Code | Cause |
|--------|------|-------|
| 400 | BAD_REQUEST | Missing JSON body or report_payload |
| 400 | COMPOSITE_KEY_INCOMPLETE | Missing required GUID fields |
| 401 | UNAUTHORIZED | Missing or invalid PAT |
| 403 | FORBIDDEN | Wrong scope |
| 403 | ORG_MISMATCH | PAT org != body org |
| 403 | GRANT_TOKEN_INVALID | HMAC verification failed |
| 403 | GRANT_EXPIRED | Grant token expired |
| 422 | VALIDATION_ERROR | Observation validation failed (details array) |

---

### Provider Feed

```
GET /api/v1/provider/feed
```

**Auth**: PAT with `read` scope

**Query parameters:**
| Param | Type | Description |
|-------|------|-------------|
| `since` | ISO-8601 | Only requests updated after this time |
| `limit` | integer | Max results per page |
| `cursor` | string | Pagination cursor from previous response |

**Response 200:**
```json
{
  "requests": [
    {
      "request_guid": "uuid",
      "provider_guid": "uuid",
      "status": "active",
      "created_at": "2026-03-26T07:00:00Z"
    }
  ],
  "cursor": "2",
  "has_more": false
}
```

---

### Download Bundle

```
GET /api/v1/provider/download/{service_request_guid}
```

**Auth**: PAT with `read` scope

**Response 200:** Full FHIR Bundle with ServiceRequest, CarePlan, grant_token.

---

### Acknowledge Receipt

```
POST /api/v1/provider/receipt/{receipt_token}/ack
```

**Auth**: PAT with `write` scope

**Response 200:**
```json
{
  "status": "acknowledged",
  "receipt_token": "uuid"
}
```

---

### Vector Queries (Experimental)

```
GET /api/v1/vectors/by-patient/{patient_guid}
GET /api/v1/vectors/by-careplan/{careplan_guid}
POST /api/v1/vectors/similar          (body: { "context": {}, "limit": 10 })
POST /api/v1/vectors/resolve/{sr_guid}  (trigger vectorization)
```

No auth required (will be added).

---

## Error Response Format

All API errors follow a consistent format:

```json
{
  "code": "ERROR_CODE",
  "message": "Human-readable description",
  "details": []
}
```

The `details` array is only present for validation errors (422).

---

## Receipt Protocol

The gateway sends receipts to provider.pdhc after processing each submission:

**Accepted data:**
```json
{
  "receipt_guid": "uuid",
  "service_request_guid": "uuid",
  "patient_guid": "uuid",
  "provider_org_guid": "uuid",
  "contract_guid": "uuid",
  "accepted": true,
  "observations_stored": 3,
  "accepted_at": "2026-03-26T07:00:00Z",
  "payload_hash": "sha256-hex"
}
```

**Rejected data:**
```json
{
  "receipt_guid": "uuid",
  "service_request_guid": "uuid",
  "accepted": false,
  "rejection_code": "VALIDATION_ERROR",
  "rejection_detail": "..."
}
```
