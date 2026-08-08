# Gateway.pdhc — User Guide (Non-Technical)

## What is the Gateway?

The PDHC Gateway is the central point where healthcare providers submit their measurement data back to the healthcare system. When a doctor creates a care plan for a patient and assigns a provider to carry out measurements, the provider uses the gateway to report results.

The gateway does not keep the measurements itself. Once it has validated an incoming report, it hands the data on to the clinical data repository (cdr1), which becomes the single, permanent home for every observation. The gateway keeps only the bookkeeping it needs to do its job: who submitted what, whether it was accepted, and whether it has been forwarded onward.

## How it works

1. **A care plan is created** — A doctor creates a service request with a care plan for a patient
2. **A provider is assigned** — The request system matches the care plan to a contracted provider
3. **The provider gets a token** — The provider receives a secure access token (PAT) that proves their identity
4. **Measurements are taken** — The provider carries out the care plan activities
5. **Data is submitted** — The provider's system sends the measurement data to the gateway
6. **Receipt is issued** — The gateway validates the data and sends a receipt back to the provider
7. **Data is resolved** — The gateway connects the measurements back to the original care plan, giving clinical meaning to the raw values
8. **Data is forwarded** — A background worker forwards each accepted measurement onward to the clinical data repository (cdr1), where it is stored permanently

## Key concepts

- **PAT (Provider Access Token)**: A secure token that identifies the provider. Issued by the request system.
- **Composite key**: Four identifiers (patient, contract, organisation, service request) plus a cryptographic grant token that prove the provider is authorized to submit this specific data. The gateway checks the grant token by asking the request system — the secret used to verify it never leaves the request system.
- **Receipt**: Confirmation sent back to the provider that data was received (accepted or rejected).
- **GUID chain resolution**: The process of connecting a measurement value back through the care plan to the original clinical template, giving it medical meaning.
- **Forwarding queue**: Every accepted measurement is placed on an internal queue and forwarded to the clinical data repository (cdr1) by a background worker. If cdr1 is briefly unreachable, the worker retries automatically.

## Reading observations back (`GET /api/v1/observations`)

The gateway no longer has a page for browsing stored observations, because it no longer stores them — cdr1 does. Instead, the gateway offers a machine-to-machine read endpoint for the analysis phase.

`GET /api/v1/observations?organization=<org_guid>` is used by analysis-phase tools (such as analyse.pdhc) to pull the observations belonging to one organisation. When a request comes in, the gateway:

- checks the caller's single sign-on (SSO) token and that they have analysis-phase access,
- confirms the caller is allowed to see the requested organisation (an administrator can read across organisations only with a written justification, which is recorded),
- works out which service requests belong to that organisation,
- forwards the query to the analysis layer (analyse.pdhc), which gathers the matching observations from across the CDRs,
- removes any rows the patient has blocked from that provider (spärr), and
- records the read in the audit trail.

This is an API used by other systems, not a web page a person browses.

## Cache Management (`/admin/cache`)

The gateway caches upstream lookups (service request context, contract scope, grant validation, GUID resolution) so it doesn't re-fetch on every request. Caches expire automatically via a time-to-live (TTL), but sometimes a stale entry causes problems — for example, if a service request's plan was updated upstream but the gateway still has the old version cached.

**When to use it:**
- Observations are failing validation and you suspect the gateway has outdated transaction or contract data
- You've changed something in request.pdhc or contract.pdhc and need the gateway to pick it up immediately
- You see "VALIDATION_ERROR" or "SCOPE_VIOLATION" rejections that shouldn't be happening

**What you can do:**
- **Flush stale** — removes only entries that have exceeded their TTL. Safe, no disruption. The gateway will re-fetch from upstream on the next request that needs the data.
- **Flush all** (per type or global) — removes all cached entries. Causes a brief increase in upstream calls as the cache refills. Use when you need to force a complete refresh.

Cache entries are grouped by source type (for example the parts of the GUID chain the gateway resolves). Each entry carries its own TTL — GUID-resolution lookups default to one hour, grant validations to 60 seconds. The cache lives in the database, not in memory, so flushing it never requires a restart.

## Health Report (`/admin/health-report`)

Live connectivity check for all upstream services the gateway depends on. Shows:

- **Service probes** — whether the gateway's own database, request.pdhc, contract.pdhc, and sso.pdhc are reachable, with latency
- **Recent errors (24h)** — PAT rejections, report rejections, and push delivery failures from the audit log

**When to use it:**
- After a deploy or restart, to verify all connections are working
- When providers report submission failures — check if an upstream service is down
- To diagnose patterns in rejections (e.g. a specific provider org getting repeated PAT rejections)

Click "Re-check now" to re-probe all services.

## Security

- All data transmission requires authentication
- Provider identity is verified cryptographically, never from request parameters; the grant-token secret is held only by request.pdhc
- All actions are logged in a GDPR-compliant audit trail
- Patient data is identified by secure identifiers, not personal information
- Analysis-phase reads are single-sign-on gated and org-scoped; cross-organisation admin reads require a recorded justification
