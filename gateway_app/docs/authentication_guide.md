# Gateway.pdhc — Authentication Procedure

## Overview

The gateway uses a two-layer authentication model:

1. **PAT (Provider Access Token)** — proves the provider's identity
2. **Composite key with HMAC grant** — proves authorization for the specific data exchange

Both layers must pass before any data is accepted.

## Layer 1: Provider Access Token (PAT)

### How PATs are issued

1. A contract is established between the healthcare organisation and the provider
2. The request system (request.pdhc) issues a PAT for the provider
3. The PAT is delivered securely to the provider
4. The provider includes it in all API calls to the gateway

### Using the PAT

Include in the `X-Provider-Token` header:

```
POST /api/v1/provider/report/sr-abc-123
X-Provider-Token: eyJhbGciOiJIUzI1NiJ9...
Content-Type: application/json
```

### PAT scopes

| Scope | Access |
|-------|--------|
| `read` | Can poll feed, download bundles |
| `write` | Can submit reports, acknowledge receipts |
| `read,write` | Full access |

### PAT validation flow

```
Provider → Gateway → request.pdhc (validate) → Gateway (cache result)
```

The gateway validates the PAT by calling request.pdhc upstream. Valid results are cached locally to reduce upstream calls.

## Layer 2: Composite Key + HMAC Grant

### Purpose

Even with a valid PAT, the provider must prove they are authorized to submit data for this specific patient + contract + service request combination.

### The composite key

Four GUIDs that uniquely identify the data exchange:

| Field | Source |
|-------|--------|
| `service_request_guid` | URL path parameter |
| `patient_guid` | Request body |
| `organisation_guid` | Request body (must match PAT) |
| `contract_guid` | Request body |

### The grant token

An HMAC-SHA256 digest that proves the request system authorized this exchange:

```
grant_token = HMAC-SHA256(
    key = HMAC_SECRET,
    message = "{sr_guid}:{patient_guid}:{org_guid}:{contract_guid}:{expires_iso}"
)
```

The HMAC_SECRET is shared between request.pdhc (issuer) and gateway.pdhc (validator).

### Validation steps

1. All 4 GUIDs present in the request
2. `organisation_guid` matches the PAT's provider org (preventing impersonation)
3. Grant token not expired (if `expires_at` provided)
4. HMAC digest matches (timing-safe comparison)

### Example

```json
{
  "patient_guid": "a1b2c3d4-...",
  "contract_guid": "e5f6g7h8-...",
  "organisation_guid": "i9j0k1l2-...",
  "grant_token": "3f2a8b1c9d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0",
  "report_payload": { ... }
}
```

## Security guarantees

- **Provider identity from PAT only** — never from request parameters
- **Timing-safe HMAC comparison** — prevents timing attacks
- **HMAC_SECRET separate from Flask SECRET_KEY** — defense in depth
- **All events audited** — with patient GUID, IP address, correlation ID
- **GDPR compliant** — audit trail includes data_subject_guid for all operations
