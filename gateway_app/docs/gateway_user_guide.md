# Gateway.pdhc — User Guide (Non-Technical)

## What is the Gateway?

The PDHC Gateway is the central point where healthcare providers submit their measurement data back to the healthcare system. When a doctor creates a care plan for a patient and assigns a provider to carry out measurements, the provider uses the gateway to report results.

## How it works

1. **A care plan is created** — A doctor creates a service request with a care plan for a patient
2. **A provider is assigned** — The request system matches the care plan to a contracted provider
3. **The provider gets a token** — The provider receives a secure access token (PAT) that proves their identity
4. **Measurements are taken** — The provider carries out the care plan activities
5. **Data is submitted** — The provider's system sends the measurement data to the gateway
6. **Receipt is issued** — The gateway validates the data and sends a receipt back to the provider
7. **Data is resolved** — The gateway connects the measurements back to the original care plan, giving clinical meaning to the raw values

## Key concepts

- **PAT (Provider Access Token)**: A secure token that identifies the provider. Issued by the request system.
- **Composite key**: Four identifiers (patient, contract, organisation, service request) plus a cryptographic grant token that prove the provider is authorized to submit this specific data.
- **Receipt**: Confirmation sent back to the provider that data was received (accepted or rejected).
- **GUID chain resolution**: The process of connecting a measurement value back through the care plan to the original clinical template, giving it medical meaning.

## The observations page

The gateway provides a web interface at `/observations` where administrators can:
- View all incoming data with timestamps
- Filter by patient identifier
- See whether receipts have been sent
- See whether the data has been resolved and connected to care plans
- Click individual observations to see full details including the FHIR JSON

## Cache Management (`/admin/cache`)

The gateway caches upstream lookups (service request context, contract scope, PAT validation results) so it doesn't re-fetch on every request. Caches expire automatically via TTL, but sometimes a stale entry causes problems — for example, if a service request's plan was updated upstream but the gateway still has the old version cached.

**When to use it:**
- Observations are failing validation and you suspect the gateway has outdated transaction or contract data
- You've changed something in request.pdhc or contract.pdhc and need the gateway to pick it up immediately
- You see "VALIDATION_ERROR" or "SCOPE_VIOLATION" rejections that shouldn't be happening

**What you can do:**
- **Flush stale** — removes only entries that have exceeded their TTL. Safe, no disruption. The gateway will re-fetch from upstream on the next request that needs the data.
- **Flush all** (per type or global) — removes all cached entries. Causes a brief increase in upstream calls as the cache refills. Use when you need to force a complete refresh.

Cache types:
- `sr_context` — service request transactions and goals (TTL: 1 hour)
- `contract_scope` — obligatory/optional concepts per contract (TTL: 60 seconds)
- `pat_validation` — provider token validation results (TTL: 1 hour)
- `service_request` — full service request data (TTL: 1 hour)

## Health Report (`/admin/health-report`)

Live connectivity check for all upstream services the gateway depends on. Shows:

- **Service probes** — whether request.pdhc, contract.pdhc, sso.pdhc, and the local database are reachable, with latency and database status
- **Recent errors (24h)** — PAT rejections, report rejections, and push delivery failures from the audit log

**When to use it:**
- After a deploy or restart, to verify all connections are working
- When providers report submission failures — check if an upstream service is down
- To diagnose patterns in rejections (e.g. a specific provider org getting repeated PAT rejections)

Click "Re-check now" to re-probe all services.

## Security

- All data transmission requires authentication
- Provider identity is verified cryptographically, never from request parameters
- All actions are logged in a GDPR-compliant audit trail
- Patient data is identified by secure identifiers, not personal information
