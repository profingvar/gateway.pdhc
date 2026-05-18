# gateway.pdhc — Deployment Plan

Observation ingestion gateway for the PDHC platform.
Ports: 9050 (Flask), 9051 (PostgreSQL), 9052–9053 reserved.
App folder: `gateway_app/`
FHIR R5 compliant. Dockerized. Consumes data from `provider.pdhc`.

---

## Role in the PDHC ecosystem

Gateway.pdhc is the **inbound data gateway** — it receives observations and reports submitted by provider organisations. Provider.pdhc (the provider portal) submits structured FHIR Observation data after completing tasks dispatched via request.pdhc.

**Access control:** Providers authenticate with a PAT (Provider Access Token) **issued by request.pdhc** — the gateway does not issue tokens itself. It validates the PAT and the composite key that request.pdhc created when the data exchange was authorized.

**GUID chain resolution:** Each incoming observation carries GUIDs that reference upstream resources. The gateway resolves the chain:

```
observation.transaction_guid  →  transaction (from careplan)
    → careplan_guid           →  CarePlan (instantiated from PlanDefinition)
        → plandef_guid        →  PlanDefinition (the clinical template)
```

This reconstruction gives meaning to raw measurement values — a numeric `72` becomes "resting heart rate, expected range 60–100, part of cardiovascular monitoring plan, prescribed by Dr X." The gateway fetches this context from request.pdhc at ingestion time.

**Vector storage:** The reconstructed context (observation + transaction metadata + careplan context + plandefinition semantics) is stored as **vectors** in the gateway database. The vector design is experimental and will be iterated during the build of this repo. Initial approach: PostgreSQL with pgvector extension, embeddings generated from the resolved GUID chain context.

**Data consumed from provider.pdhc:**

The provider submits reports via `POST /api/v1/provider/report/{service_request_guid}` with:
- `X-Provider-Token` header (PAT issued by request.pdhc)
- Composite key body: `patient_guid`, `contract_guid`, `organisation_guid`, `grant_token`
- `report_payload` containing FHIR Observations (guided mode) or freeform JSON (manual mode)

The gateway validates the PAT and composite key (4 GUIDs + HMAC grant token) before accepting data. See `../css_instrux/provider_data_delivery_plan.md` for the full security architecture.

**Data flow:**
```
provider.pdhc                    gateway.pdhc                     request.pdhc
    │                                │                                │
    │  POST /provider/report/<guid>  │                                │
    │  X-Provider-Token: <PAT>       │                                │
    │  { composite key + payload }   │                                │
    ├───────────────────────────────▶│                                │
    │                                ├─ validate PAT (issued by req)  │
    │                                ├─ validate composite key        │
    │                                ├─ validate FHIR R5 Observation  │
    │                                │                                │
    │                                │  resolve GUID chain            │
    │                                ├───────────────────────────────▶│
    │                                │  ◀─ careplan + plandef context │
    │                                │                                │
    │                                ├─ reconstruct + vectorize       │
    │                                ├─ store observation + vectors   │
    │                                ├─ audit log                     │
    │                                │                                │
    │    202 Accepted + receipt       │                                │
    │◀───────────────────────────────┤                                │
    │                                │                                │
    │  receipt pushed to provider    │                                │
    │◀───────────────────────────────┤                                │
    │  POST /api/v1/receipts/ingest  │                                │
    │  { receipt_guid, sr_guid, … }  │                                │
```

---

## Phase 1 — Foundation

### 1.a Project scaffold
- Create `gateway_app/` with Flask app structure
- Create venv inside `gateway_app/venv/`
- Create `requirements.txt`
- Create `CLAUDE.md` referencing `../css_instrux/repo_css.md`
- Copy `pdhc.css` into `gateway_app/static/css/`

### 1.b Docker and database setup
- `Dockerfile` for Flask app
- `docker-compose.yml` with PostgreSQL (port 9051) and Flask (port 9050)
- `.env` file with DB credentials, Flask secret, HMAC_SECRET, bootstrap SU API key
- PostgreSQL schema migration (Flask-Migrate / Alembic)

### 1.c Database schema
- `inbound_observations` — guid, service_request_guid, transaction_guid, concept_guid, patient_guid, provider_org_guid, contract_guid, grant_guid, fhir_observation_json, value, response_type, validation_status, received_at, created_at
- `observation_vectors` — guid, observation_guid (FK), careplan_guid, plandef_guid, transaction_guid, resolved_context_json (the full reconstructed chain), embedding (vector, pgvector), vector_model, created_at. **Design is experimental — schema will evolve.**
- `guid_resolution_cache` — guid, source_guid, source_type (transaction/careplan/plandef), resolved_json, fetched_from (request.pdhc URL), fetched_at, ttl. Caches upstream lookups to avoid repeated calls.
- `validation_log` — guid, observation_guid, validation_type, passed, error_details, validated_at
- `audit_log` — guid, event_type, actor_guid, data_subject_guid (patient), receipt_token, payload_snapshot, ip_address, correlation_id, created_at
- All cross-table references use GUIDs (Rule 18)
- PostgreSQL with **pgvector** extension for vector storage

### 1.d start.sh
- Kill processes on ports 9050–9053
- Activate venv
- Start Docker (PostgreSQL) if not running
- Start Flask app
- Ctrl+C graceful shutdown and deactivate

---

## Phase 2 — PAT validation and composite key verification

### 2.a PAT validation middleware
- PATs are **issued by request.pdhc**, not by the gateway
- Gateway validates `X-Provider-Token` header by calling request.pdhc's token validation endpoint (or using a shared HMAC_SECRET for offline verification)
- On valid PAT: extract `provider_org_guid`, `contract_guid`, `scopes`, `delivery_mode`
- Provider identity is **never** taken from request parameters
- Cache validated PAT data locally in `guid_resolution_cache` to reduce upstream calls
- Tests: valid PAT, missing PAT (401), invalid PAT (401), expired PAT (401), revoked PAT (401)

### 2.b Composite key validation service
- Verify all 4 GUIDs (service_request, patient, organisation, contract) match the grant record
- HMAC-SHA256 validation: `HMAC(key=HMAC_SECRET, msg="{sr_guid}:{patient_guid}:{org_guid}:{contract_guid}:{expires_iso}")`
- HMAC_SECRET is shared with request.pdhc (the grant issuer)
- Check grant not expired, not revoked, not over max_uses
- Increment `used_count` on successful validation
- Tests: valid composite key, wrong patient (403), forged grant_token (403), expired grant (403), used-up grant (403)

---

## Phase 3 — Inbound observation reception

### 3.a Report submission endpoint
- `POST /api/v1/provider/report/{service_request_guid}`
- Auth: PAT + composite key (defense in depth per provider_data_delivery_plan.md Section 6)
- Validation chain:
  1. PAT validates → `g.provider_org_guid` must match `body.organisation_guid`
  2. ServiceRequest GUID exists
  3. Contract match exists linking SR to org via contract
  4. DataExchangeGrant exists, HMAC validates, not expired/revoked
  5. Audit logged with `data_subject_guid` (patient)
- Accept `report_payload` with FHIR Observations (guided) or freeform JSON (manual)
- Store in `inbound_observations`
- Return 202 Accepted with receipt
- Tests: valid submission, missing composite key field (400), PAT/org mismatch (403), HMAC forged (403), duplicate submission (idempotent)

### 3.b FHIR R5 Observation validation
- Validate `report_payload.observations` array against FHIR R5 Observation schema
- Each observation must have: `transaction_guid`, `concept_guid`, `value`, `response_type`
- Validate response_type constraints (numeric range, categorical from valueset, text length)
- Store validation results in `validation_log`
- Tests: valid observations, missing required field (422), invalid response_type (422), out-of-range numeric (422)

### 3.c Receipt and acknowledgement
- `POST /api/v1/provider/receipt/{receipt_token}/ack` — acknowledge delivery receipt
- Generate immutable receipt records for every accepted submission
- Tests: receipt creation, receipt acknowledgement, receipt immutability

### 3.d Receipt delivery to provider.pdhc (tilläggsuppdrag)
- When the gateway accepts an observation report, it **pushes a receipt** to provider.pdhc
- Gateway calls `POST {PROVIDER_SERVICE_URL}/api/v1/receipts/ingest` with:
  ```json
  {
    "receipt_guid": "<generated>",
    "service_request_guid": "<sr_guid>",
    "patient_guid": "<patient_guid>",
    "provider_org_guid": "<org_guid>",
    "contract_guid": "<contract_guid>",
    "observations_stored": 3,
    "accepted_at": "2026-03-26T07:00:00Z",
    "payload_hash": "<sha256>"
  }
  ```
- Gateway uses an internal service key (not PAT) to authenticate to provider.pdhc
- On delivery failure: log and retry (fire-and-forget; do not block the 202 response to the provider)
- **Provider.pdhc recoding needed:** provider.pdhc must add a `POST /api/v1/receipts/ingest` endpoint that stores receipts and displays them in the provider dashboard. Instructions in `docs/provider_receipt_protocol.md`.
- Tests: receipt push on successful ingestion, push failure logged, receipt payload structure

---

## Phase 4 — GUID chain resolution and vector storage

### 4.a GUID resolution service
- On each accepted observation, resolve the GUID chain from request.pdhc:
  1. `transaction_guid` → fetch transaction definition (concept, response_type, unit, valueset, required flag)
  2. `careplan_guid` (from service request) → fetch CarePlan (activities, goals, patient context)
  3. `plandef_guid` (from careplan) → fetch PlanDefinition (the clinical template: SNOMED/LOINC codes, expected ranges, clinical purpose)
- Cache resolved data in `guid_resolution_cache` with TTL (avoid hitting request.pdhc on every observation)
- Graceful degradation: if upstream is unreachable, store observation with `resolution_status=pending`, retry later
- Tests: full chain resolution, cache hit, cache miss, upstream unavailable (queued), stale cache refresh

### 4.b Vector construction (experimental)
- Combine resolved context into a structured representation:
  - Observation value + unit + response_type
  - Transaction concept (SNOMED/LOINC code + display name)
  - CarePlan context (what clinical goal this serves)
  - PlanDefinition semantics (the clinical template this derives from)
  - Patient and provider identifiers
- Generate embedding vector from this context
- Store in `observation_vectors` using pgvector
- **This design is experimental** — vector dimensions, embedding model, and similarity metrics will be iterated during development
- Tests: vector generation from resolved context, vector storage/retrieval, similarity search basic test

### 4.c Vector query endpoints (experimental)
- `GET /api/v1/vectors/by-patient/{patient_guid}` — all vectors for a patient
- `GET /api/v1/vectors/by-careplan/{careplan_guid}` — all vectors under a careplan
- `GET /api/v1/vectors/similar` — similarity search across observations
- These endpoints will evolve as the vector design matures
- Tests: query by patient, query by careplan, similarity search

---

## Phase 5 — Provider feed (poll mode support)

### 5.a Metadata feed endpoint
- `GET /api/v1/provider/feed` — list ServiceRequests for this provider (PAT auth)
- **Metadata only** — no patient names, diagnoses, or clinical data (GDPR data minimization)
- Returns: service_request_guid, status, title, created_at, download_url
- Query params: `since` (ISO-8601), `limit`
- Tests: feed with data, empty feed, pagination, auth enforcement

### 5.b Bundle download endpoint
- `GET /api/v1/provider/download/{service_request_guid}` — full FHIR Bundle + grant_token
- PAT auth + confirms match exists for this org + SR
- Issues DataExchangeGrant if none exists
- Audit logged: `bundle.downloaded` with `data_subject_guid`
- Tests: download, org mismatch (403), nonexistent SR (404), audit trail verified

---

## Phase 6 — Push delivery (push mode support)

### 6.a Push service
- When a ServiceRequest is finalized and matched, build FHIR Bundle:
  - ServiceRequest envelope with contained CarePlan + Goals + Patient
  - Meta tags: receipt_token, grant_token (4 GUIDs + HMAC)
- POST to provider's `push_endpoint_url` (from PAT record)
- Headers: `X-Push-Secret` (mutual auth), `X-Correlation-Id` (audit trace)
- Update ServiceRequestReceipt: delivery_status
- Tests: push delivery, push failure retry, mutual auth, correlation ID propagation

---

## Phase 7 — Error handling, audit, hardening

### 7.a Standardized error responses
- Format: `{ "code": "...", "message": "...", "details": [...] }`
- HTTP status mapping: 400/401/403/404/409/422/500
- Tests: each error code scenario

### 7.b Audit trail (GDPR compliant)
- All events logged per provider_data_delivery_plan.md Section 7:
  - `pat.issued`, `pat.revoked`, `pat.validated`, `pat.rejected`
  - `grant.issued`, `grant.used`, `grant.expired`
  - `feed.accessed`, `bundle.downloaded`, `bundle.pushed`
  - `report.received`
- All audit records include `data_subject_guid` (patient GUID)
- `X-Correlation-Id` header propagated across service boundaries
- Tests: audit trail completeness, data_subject_guid present, correlation ID traced

### 7.c Security hardening
- PAT storage: bcrypt hashed, rotation support, expiry, revocation
- Rate limiting per PAT (not just per IP)
- Grant expiry cleanup job
- HMAC_SECRET separate from Flask SECRET_KEY
- Tests: key rotation, rate limiting, grant cleanup

---

## Phase 8 — Testing and integration

### 8.a pytest suite for all endpoints
- Unit tests for each service
- Results stored in `./results/<timestamp>_results/` (Rule 11)

### 8.b End-to-end integration test
- Full flow: provider.pdhc submits report → gateway validates composite key → stores observation → returns receipt
- Poll flow: provider polls feed → downloads bundle → completes task → submits report
- Push flow: gateway pushes bundle → provider receives → acknowledges

### 8.c Full endpoint test script (Rules 9, 20)
- Script testing all API endpoints per capability statement

---

## Phase 9 — Frontend (gateway admin dashboard)

### 9.a Inbound observations viewer (tilläggsuppdrag 2 — provisional)
- `GET /observations` — list all inbound observations with pagination
  - Filterable by patient GUID
  - Shows: time received, patient GUID (truncated), concept, value, provider org, receipt status, resolution status
  - Click "View" to see full detail
- `GET /observations/<guid>` — detail view
  - Observation data: value, response_type, validation/resolution status, receipt status
  - Identifiers: patient, provider, contract, service request, transaction, concept GUIDs
  - Resolved context (if vectorized): concept name, activity description, careplan/plandefinition titles, embedding dimensions
  - Audit trail: all events for this service request
  - Raw FHIR Observation JSON
- Nav link added to base.html
- Dashboard updated to show current phase status + link to observations

### 9.b Additional dashboard pages (future)
- PAT management (issue, list, revoke)
- Audit log viewer
- Grant status dashboard

---

## Phase 10 — Deployment preparation

### 10.a Documentation
- API contract documentation
- Auth scope matrix
- FHIR capability statement

### 10.b Server preparation
- `safe_restart.sh` for web instance
- `.env` fully prepared with bootstrap SU user (Rule 23)
- Reverse proxy caution per Rule 22

### 10.c Web deployment (Rule 12)
- Download current server state before changes
- Compare with local
- Present comparison, then operator applies changes
- No ssh/scp from the plan

---

## API Key Management Rules (Rule 8)

- **PAT issuance**: PATs are created by request.pdhc, not by the gateway. The gateway validates them.
- **PAT validation**: gateway verifies the token against request.pdhc (or via shared HMAC_SECRET for offline verification), caches result
- **Grant tokens**: HMAC-SHA256, issued by request.pdhc, validated by gateway using shared HMAC_SECRET
- **Bootstrap**: initial SU key for gateway admin seeded from `.env` on first run
- **Rotation/revocation**: managed on request.pdhc; gateway cache respects TTL and checks revocation status

---

## .env variables

```bash
# Database
DATABASE_URL=postgresql://gateway_user:password@localhost:9051/gateway_db
FLASK_SECRET_KEY=<random-64-char>

# Security
HMAC_SECRET=<random-64-char-separate-from-flask-secret>
PAT_DEFAULT_EXPIRY_DAYS=365
GRANT_EXPIRY_HOURS=72
GRANT_MAX_USES=10

# Bootstrap
BOOTSTRAP_SU_API_KEY=<initial-superuser-key>

# Upstream (request.pdhc) for GUID resolution
REQUEST_SERVICE_URL=https://request.pdhc.se/api/v1
GUID_CACHE_TTL_SECONDS=3600

# Push settings
PUSH_TIMEOUT_SECONDS=30
PUSH_RETRY_COUNT=3

# Downstream (provider.pdhc) for receipt delivery
PROVIDER_SERVICE_URL=http://localhost:9070/api/v1

# Vector storage (experimental)
PGVECTOR_DIMENSIONS=384
EMBEDDING_MODEL=local

# Flask
FLASK_ENV=development
FLASK_DEBUG=1
```

---

## Endpoint summary

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| POST | `/api/v1/provider/report/<sr_guid>` | PAT + grant | Receive observation report from provider |
| GET | `/api/v1/provider/feed` | PAT | List SRs for provider (metadata only) |
| GET | `/api/v1/provider/download/<sr_guid>` | PAT | Download full FHIR Bundle + grant |
| POST | `/api/v1/provider/receipt/<token>/ack` | PAT | Acknowledge delivery receipt |
| GET | `/api/v1/vectors/by-patient/<guid>` | SU auth | Vectors for a patient (experimental) |
| GET | `/api/v1/vectors/by-careplan/<guid>` | SU auth | Vectors under a careplan (experimental) |
| GET | `/api/v1/vectors/similar` | SU auth | Similarity search (experimental) |
| GET | `/api/v1/health` | None | Health check |
