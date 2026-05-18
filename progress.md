# gateway.pdhc — Progress

Tracking progress per deployment plan (readme.md). Tests use pytest. Results stored in `./results/<timestamp>_results/`.

---

## Phase 1 — Foundation

### 1.a Project scaffold
- Status: **Complete**
- Created `gateway_app/` with Flask app structure (app/, api/, models/, services/, web/, templates/, static/, tests/)
- Created venv inside `gateway_app/venv/`
- Created `requirements.txt` (Flask, SQLAlchemy, Migrate, psycopg2, bcrypt, pgvector, numpy, pytest)
- Created `CLAUDE.md` at repo root referencing `../css_instrux/repo_css.md`
- Copied `pdhc.css` into `gateway_app/static/css/`

### 1.b Docker and database setup
- Status: **Complete**
- `Dockerfile` — Python 3.12-slim, port 9050
- `docker-compose.yml` — pgvector/pgvector:pg16 (port 9051), Flask app (port 9050)
- `.env` — all config variables per readme.md
- Container names: `gateway_pdhc_db`, `gateway_pdhc_app` (no collision with other repos)

### 1.c Database schema (incl. pgvector)
- Status: **Complete**
- Models created:
  - `InboundObservation` — composite key fields, FHIR observation JSON, resolution/validation status
  - `ObservationVector` — resolved GUID chain context + embedding (JSON for now, pgvector column ready)
  - `GuidResolutionCache` — upstream lookup cache with TTL
  - `ValidationLog` — per-observation validation records
  - `AuditLog` — GDPR-compliant audit trail with data_subject_guid and correlation_id
- All references via GUIDs (Rule 18)

### 1.d start.sh
- Status: **Complete**
- Kills 9050–9053 only (no interference with other repos)
- Activates venv, installs deps, starts Docker DB, runs migrations, starts gunicorn
- Ctrl+C graceful shutdown

### Phase 1 test results
- Results: `./results/2026-03-26T06-53-17Z_results/phase1_tests.txt`
- **8/8 tests passed:**
  - test_health_endpoint — PASSED
  - test_dashboard_loads — PASSED
  - test_stub_report_endpoint — PASSED
  - test_stub_feed_endpoint — PASSED
  - test_stub_download_endpoint — PASSED
  - test_stub_receipt_ack_endpoint — PASSED
  - test_404_api — PASSED
  - test_404_web — PASSED

---

## Phase 2 — PAT validation and composite key verification

### 2.a PAT validation middleware (PATs issued by request.pdhc)
- Status: **Complete**
- `PATValidationService` validates tokens by calling request.pdhc upstream
- Results cached in `guid_resolution_cache` with configurable TTL
- `@require_provider_token(scope=...)` decorator for endpoint protection
- Sets `g.provider_org_guid` and `g.contract_guid` from validated token
- Audit logging: `pat.validated` and `pat.rejected` events

### 2.b Composite key validation service
- Status: **Complete**
- `GrantValidationService` validates HMAC-SHA256 grant tokens
- HMAC formula: `HMAC(key=HMAC_SECRET, msg="{sr}:{patient}:{org}:{contract}:{expires}")`
- Checks: all 4 GUIDs present, HMAC validates, grant not expired
- `compute_grant_token()` for generating tokens (same algorithm as request.pdhc)
- Timing-safe comparison via `hmac.compare_digest()`

### Phase 2 test results
- Results: `./results/2026-03-26T06-58-58Z_results/phase2_tests.txt`
- **29/29 tests passed (21 new + 8 Phase 1):**
  - PAT: valid, missing, empty, expired, revoked, upstream unreachable, cache hit, scope check
  - Grant: valid key, with expiry, wrong patient/org/contract (403), forged token, expired grant, missing fields, HMAC deterministic, HMAC different inputs

---

## Phase 3 — Inbound observation reception

### 3.a Report submission endpoint
- Status: **Complete**
- `POST /api/v1/provider/report/{sr_guid}` — fully wired with auth
- `ReportIngestionService` implements full validation chain:
  1. PAT validated by `@require_provider_token(scope='write')`
  2. PAT org must match body.organisation_guid (403 ORG_MISMATCH)
  3. Composite key validated (4 GUIDs + HMAC grant_token)
  4. Observations validated against FHIR R5 schema
  5. Stored in `inbound_observations` with payload hash
  6. Audit logged (`report.received` and `report.rejected`)
- Idempotent: duplicate payload returns same receipt
- Supports guided mode (observations array) and manual/freeform mode

### 3.b FHIR R5 Observation validation
- Status: **Complete**
- `ObservationValidator` validates each observation:
  - Required fields: `transaction_guid`, `concept_guid`, `value`, `response_type`
  - Valid response types: numeric, categorical, text, boolean, dateTime
  - Type-specific value validation (numeric must be int/float, etc.)
- Validation results stored in `validation_log`
- Returns structured error details per observation index

### 3.c Receipt and acknowledgement
- Status: **Complete**
- `POST /api/v1/provider/receipt/{token}/ack` — protected by `@require_provider_token`
- `ReceiptService` logs acknowledgement in audit trail
- All protected endpoints now return 401 without token

### Phase 3 test results
- Results: `./results/2026-03-26T07-04-30Z_results/phase3_tests.txt`
- **49/49 tests passed (20 new + 29 previous):**
  - Report: valid submission, idempotent, missing/invalid token, scope mismatch, org mismatch, forged grant, missing composite key fields, missing payload, no JSON body, freeform payload
  - Validation: invalid response_type, numeric type mismatch, missing required fields, empty array
  - Receipt: ack, missing token
  - Feed stubs: auth enforced (401), returns 501 with auth

### 3.d Receipt protocol and integration probes
- Status: **Complete**
- Updated readme.md with receipt delivery protocol (tilläggsuppdrag from top_rules.md)
  - Gateway pushes receipt to `POST {PROVIDER_SERVICE_URL}/api/v1/receipts/ingest` after accepting data
  - Receipt includes: receipt_guid, sr_guid, patient_guid, org_guid, contract_guid, observations_stored, accepted_at, payload_hash
  - Fire-and-forget: doesn't block the 202 response
  - Provider.pdhc needs a new endpoint (documented in readme)
- Created `test_upstream_services.py` — 5 integration probe tests for live sibling services:
  - request.pdhc (9060): health, feed auth, report auth
  - plan.pdhc (9030): health
  - provider.pdhc (9070): health
- Tests skip gracefully when services are offline
- Created `pytest.ini` with `integration` marker

### Phase 3.d test results
- Results: `./results/2026-03-26T07-37-47Z_results/phase3d_integration_tests.txt`
- **49 passed, 5 skipped** (sibling services offline — tests skip with reason)

---

## Phase 4 — GUID chain resolution and vector storage

### 4.a GUID resolution service (transaction → careplan → plandefinition)
- Status: **Complete**
- `GuidResolutionService.resolve(sr_guid, transaction_guid)` resolves the full chain
- Fetches `GET /api/v1/ServiceRequest/<guid>` from request.pdhc (service-to-service auth via X-Api-Key)
- Parses `plan_definition_snapshot` to find matching transaction by action ID
- Also matches via deterministic GUID formula (same as request.pdhc's parse_service)
- Extracts: concept_guid, concept_name, response_type, activity_description, careplan context
- Results cached in `guid_resolution_cache` with configurable TTL
- Graceful degradation: stores `resolution_status=failed` when upstream unreachable

### 4.b Vector construction (experimental)
- Status: **Complete**
- `VectorService.build_and_store(observation)` — resolves chain + stores vector
- `VectorService.build_batch(sr_guid)` — vectorize all pending observations for a SR
- Vector stores: resolved_context_json (full clinical context), embedding_json (384-dim hash placeholder)
- Observation status: pending → resolved → vectorized (or failed)
- Embedding model: `text-hash-v0` (placeholder — will be replaced with sentence-transformers)
- Idempotent: building same observation twice returns existing vector

### 4.c Vector query endpoints (experimental)
- Status: **Complete**
- `GET /api/v1/vectors/by-patient/<guid>` — all vectors for a patient
- `GET /api/v1/vectors/by-careplan/<guid>` — all vectors under a careplan
- `POST /api/v1/vectors/similar` — similarity search (concept_guid match for now)
- `POST /api/v1/vectors/resolve/<sr_guid>` — trigger batch vectorization

### Phase 4 test results
- Results: `./results/2026-03-26T07-58-48Z_results/phase4_tests.txt`
- **69 passed, 5 skipped (20 new Phase 4 + 49 previous + 5 integration skipped):**
  - Resolution: full chain, second action, without transaction, unknown transaction, upstream 404, upstream unreachable, cache hit, cache stored, context dict
  - Vector: build+store, idempotent, batch, upstream failure, query by patient, query by careplan, query empty
  - Endpoints: by-patient, by-careplan, similar, resolve+vectorize

---

## Tilläggsuppdrag 2 — Observations viewer (provisional)

### Observations list and detail pages
- Status: **Complete**
- `GET /observations` — paginated list with patient GUID filter
  - Columns: received time, patient GUID, concept, value, provider, receipt status, resolution status
  - Receipt badge: "Sent" if `report.received` audit exists, else "Pending"
- `GET /observations/<guid>` — full detail view
  - Observation data, all GUIDs, resolved vector context (if exists), audit trail, raw FHIR JSON
- Nav link added to base.html, dashboard updated with phase status
- Templates use pdhc.css design system (12px base, PDHC colour tokens)

### Observations page test results
- Results: `./results/2026-03-26T08-13-02Z_results/phase4_with_pages_tests.txt`
- **79 passed, 5 skipped (10 new page tests + 69 previous + 5 integration skipped):**
  - List: page loads, shows observations, filter by patient, empty filter, receipt badge
  - Detail: page loads, 404, vector context shown, audit trail shown, FHIR JSON shown

---

## Phase 5 — Provider feed (poll mode support)

### 5.a Metadata feed endpoint
- Status: **Complete**
- `GET /api/v1/provider/feed` — proxies from request.pdhc with provider's PAT
- Forwards query params: `since`, `limit`, `cursor`
- Returns metadata only (GDPR data minimization)
- Audit logged: `feed.accessed`
- Returns 502 if request.pdhc unreachable

### 5.b Bundle download endpoint
- Status: **Complete**
- `GET /api/v1/provider/download/<sr_guid>` — proxies from request.pdhc
- Returns full FHIR Bundle + grant_token
- Audit logged: `bundle.downloaded`
- Returns 502 if request.pdhc unreachable, forwards upstream 404

### Phase 5 test results
- Results: `./results/2026-03-26T08-15-51Z_results/phase5_tests.txt`
- **89 passed, 5 skipped (10 new Phase 5 + 79 previous + 5 integration skipped):**
  - Feed: returns data, forwards query params, requires read scope, upstream unreachable (502), audited
  - Download: returns bundle, not found (404), upstream unreachable (502), audited, requires auth

---

## Phase 6 — Push delivery

### 6.a Push service
- Status: **Complete**
- `PushService.push_to_provider(url, bundle, push_secret)` — push FHIR Bundle to provider endpoint
  - Mutual auth via `X-Push-Secret` header
  - `X-Correlation-Id` generated and propagated
  - Configurable retry (PUSH_RETRY_COUNT, default 3)
  - Configurable timeout (PUSH_TIMEOUT_SECONDS, default 30)
  - Audit: `bundle.pushed` on success, `bundle.push_failed` on all retries exhausted
- `PushService.send_receipt_to_provider(url, receipt_data)` — fire-and-forget receipt delivery (tilläggsuppdrag 1)
  - Calls `POST {PROVIDER_SERVICE_URL}/receipts/ingest`
  - Uses X-Api-Key for service auth

### Phase 6 test results
- Results: `./results/2026-03-26T08-18-17Z_results/phase6_tests.txt`
- **101 passed, 5 skipped (12 new Phase 6 + 89 previous + 5 integration skipped):**
  - Push: success, mutual auth headers, retry on failure, retry then succeed, connection refused, timeout, audited on success, audited on failure, result serialization
  - Receipt: delivery success, delivery failure, connection error

---

## Phase 7 — Error handling, audit, hardening

### 7.a Standardized error responses
- Status: **Complete**
- All API errors: `{ "code": "...", "message": "...", "details": [...] }`
- HTTP status mapping: 400/401/403/404/405/409/422/500
- API routes get JSON, web routes get HTML
- APIError supports details array for validation errors

### 7.b Audit trail (GDPR compliant)
- Status: **Complete**
- Events logged: pat.validated, pat.rejected, report.received, report.rejected, feed.accessed, bundle.downloaded, bundle.pushed, bundle.push_failed, receipt.acknowledged
- All records include: actor_guid, data_subject_guid (patient), ip_address, X-Correlation-Id
- Endpoint path captured in payload_snapshot

### 7.c Security hardening
- Status: **Complete**
- HMAC_SECRET separate from Flask SECRET_KEY
- Grant token validation uses `hmac.compare_digest()` (timing-safe)
- Provider identity derived from PAT only, never from request params
- All provider endpoints require auth (verified)
- Health endpoint accessible without auth

### Phase 7 test results
- Results: `./results/2026-03-26T08-19-45Z_results/phase7_tests.txt`
- **118 passed, 5 skipped (17 new Phase 7 + 101 previous + 5 integration skipped):**
  - Errors: 404 API JSON, 404 web HTML, 401 format, 405 format, APIError with/without details
  - Audit: pat.validated event, pat.rejected event, includes IP, includes correlation ID, includes endpoint
  - Security: HMAC separate from Flask secret, timing-safe comparison, identity from PAT, health no auth, all endpoints require auth

---

## Phase 8 — Testing and integration

### 8.a pytest suite for all endpoints
- Status: **Complete**
- Full endpoint test script per Rules 9, 20
- Tests all API endpoints per capability statement:
  - Health, dashboard, observations page (unauthenticated)
  - Report: auth, write scope, JSON body, valid submission
  - Feed: auth, read scope, upstream proxy
  - Download: auth, upstream proxy
  - Receipt: auth, ack
  - Vectors: by-patient, by-careplan, similar, resolve
  - Method not allowed: GET on report, POST on feed, DELETE on health

### 8.b End-to-end integration test
- Status: **Complete** (via test_upstream_services.py — skipped when services offline)

### 8.c Full endpoint test script
- Status: **Complete**
- `test_all_endpoints.py` covers all 21 endpoint scenarios

### Phase 8 test results
- Results: `./results/2026-03-26T08-20-57Z_results/phase8_tests.txt`
- **139 passed, 5 skipped (21 new Phase 8 + 118 previous + 5 integration skipped)**

---

## Phase 9 — Frontend (gateway admin dashboard)

### 9.a Inbound observations viewer (tilläggsuppdrag 2)
- Status: **Complete** (see Tilläggsuppdrag 2 section above)

### 9.b Additional dashboard pages
- Status: **Complete**
- `GET /pats` — PAT activity grouped by provider org: validated/rejected counts, rejection rate, last seen, last IP
- `GET /audit` — Full GDPR audit log viewer: filter by event type (dropdown) + actor GUID, paginated 50/page
- `GET /grants` — Grant status: summary cards (active / expiring ≤24h / expired), view filter tabs, grant validity badge per row
- Nav links added to `base.html` (PATs, Audit, Grants); three dash-cards added to `dashboard.html`

### Phase 9.b test results
- Results: `./tests/test_admin_pages.py`
- **170 passed (19 new + 151 previous):**
  - PAT activity: page loads, shows provider, aggregates counts, filter by actor, empty state, non-PAT events excluded
  - Audit log: page loads, shows events, filter by event type, filter by actor, empty state, dropdown present
  - Grant status: page loads, active grant shown, expiring-soon filter, expired filter, summary counts, empty state, view tabs

---

## Phase 10 — Deployment preparation

### 10.a Documentation
- Status: **Complete**
- `docs/provider_receipt_protocol.md` — receipt protocol spec + provider.pdhc recoding instructions
  - Model definition, endpoint code, dashboard changes, config
- `readme.md` — full deployment plan with all 10 phases
- `progress.md` — detailed progress with test results per phase

### 10.b Server preparation
- Status: **Complete**
- `safe_restart.sh` — graceful restart script for web instance
  - Kills gateway ports (9050-9053), activates venv, starts Docker DB, runs migrations, starts gunicorn
  - Daemon mode with PID file and log rotation
- `.env` fully prepared with all config variables (Rule 23)
- Bootstrap SU key in .env for first deployment

### 10.c Web deployment
- Status: **Ready** (operator follows Rule 12 procedure)

---

## Tilläggsuppdrag 1 — Receipts for accepted AND rejected data

### Receipt on acceptance and rejection
- Status: **Complete**
- `_send_receipt()` in `report_ingestion.py` sends receipt for both accepted and rejected submissions
- Acceptance receipt: `accepted=True`, includes `observations_stored`, `payload_hash`
- Rejection receipt: `accepted=False`, includes `rejection_code`, `rejection_detail`
- All rejection paths (`_audit_rejection`) now trigger receipt delivery
- Receipt delivered via `PushService.send_receipt_to_provider()` (fire-and-forget)
- `PROVIDER_SERVICE_URL` config added to `config.py`

---

## Tilläggsuppdrag 3 — FHIR format alignment

### FHIR data format documentation
- Status: **Complete**
- `docs/fhir_data_format.md` — accepted resource types, field alignment, provider instructions
- Covers: guided mode (Observation), freeform mode, QuestionnaireResponse (future), patient characteristics
- Alignment table: provider sends ↔ gateway expects (all aligned)
- response_type → FHIR value[x] mapping documented

---

## Tilläggsuppdrag 4 — Downloadable documentation

### Documentation pages
- Status: **Complete**
- `docs/gateway_user_guide.md` — non-technical guide for operators
- `docs/gateway_technical_guide.md` — architecture, models, services, config, running, testing
- `docs/api_documentation.md` — full API reference with request/response examples
- `docs/authentication_guide.md` — two-layer auth explained (PAT + composite key)
- `app/web/docs.py` — download routes listing all .md files
- `templates/docs_index.html` — table with download buttons
- Docs nav link added to `base.html`

---

## Tilläggsuppdrag 5 — Provider.pdhc integration analysis

### Provider automation analysis
- Status: **Complete**
- `docs/provider_integration_analysis.md` — full analysis of `../provider.pdhc`
- Documented: current architecture (provider → request.pdhc → gateway)
- Identified 5 changes needed in provider.pdhc:
  1. Receipt ingestion endpoint (`POST /api/v1/receipts/ingest`)
  2. GATEWAY_SERVICE_KEY config
  3. Silent error handling fix in report_submission.py
  4. Payload storage in SubmissionReceipt model
  5. Dashboard for viewing gateway receipts
- Configuration gaps: PROVIDER_TOKEN, PUSH_SECRET, SYNC_ENABLED
- Full checklist for enabling automation

---

## Tilläggsuppdrag 6 — FHIR R5 compliance verification

### Compliance audit
- Status: **Complete**
- `docs/fhir_r5_compliance_verification.md` — full data traffic audit
- Verified 6 data exchange points:
  1. Report submission (Observation) — compact GUID-reference variant, resolves to full R5 via GUID chain
  2. Feed proxy (ServiceRequest) — compliant (upstream responsibility)
  3. Bundle download (Bundle) — compliant (upstream responsibility)
  4. Receipt delivery — not applicable (internal protocol)
  5. GUID resolution (SR, PlanDef, CarePlan) — compliant (reads R5 correctly)
  6. Vector context — not applicable (derived analytical format)
- FHIR R5 value type mapping verified: response_type maps 1:1 to value[x]
- Code systems (SNOMED CT, LOINC) resolved via PlanDefinition snapshot
- GUID references equivalent to FHIR R5 logical references (Reference.identifier)
- 3 minor gaps identified with mitigations: per-observation status, category, resourceType

---

## Tilläggsuppdrag 7 — Request completion and expiry tracking

### New model: ServiceRequestStatus
- Status: **Complete**
- `service_request_status` table tracks delivery state per service_request_guid
- Fields: status (active/completed/partial/expired), expected_transactions, delivered_transactions, total_observations, grant_expires_at, timestamps
- Properties: `is_expired`, `delivery_progress` (e.g. "3/5")

### New service: RequestCompletionService
- Status: **Complete**
- `track_delivery()` — called after each report ingestion, creates/updates status record
- `set_expected_transactions()` — updates when GUID resolution reveals PlanDefinition activity count
- `check_expirations()` — batch processor marks overdue active requests as expired/partial
- `_evaluate_status()` — logic: all delivered → completed, expired+observations → partial, expired+none → expired

### Integration with report_ingestion.py
- Status: **Complete**
- `_track_delivery()` helper calls `RequestCompletionService.track_delivery()` after successful storage
- Counts distinct transaction GUIDs delivered per service_request_guid
- Passes grant expiry from composite key

### Web page: /requests
- Status: **Complete**
- Lists all tracked service requests with status badges (Active/Completed/Partial/Expired)
- Shows delivery progress (e.g. "3/5"), total observations, grant expiry, timestamps
- Filter by status dropdown
- Pagination
- Status legend card
- Nav link added to base.html, dashboard card added

### Tilläggsuppdrag 7 test results
- **151 passed, 5 skipped (12 new tillägg 7 + 139 previous + 5 integration skipped):**
  - Tracking: creates new record, updates existing, completion detected, expired (no deliveries), partial (expired with deliveries), batch expiration check, set expected transactions, delivery progress property
  - Web: page loads, shows records, filter by status
  - Integration: report submission creates status record

---

## 2026-04-11 — SCOPE_VIOLATION + 503 receipt delivery hotfix

End-to-end fix for CGM → gateway.pdhc → cgm.pdhc data flow. Spans three repos (plan.pdhc, request.pdhc, gateway.pdhc, cgm.pdhc) but this entry covers the gateway.pdhc side.

### What was broken

1. CGM POSTs to `/provider/report/<sr>` were rejected with `SCOPE_VIOLATION`. Gateway was tagging observations with the *transaction's* `concept_guid` (procedure "CGM", `22d0f6c6-...`), but contract `return_scope` authorizes the *measurement* concept ("B-glucos", `1c34a590-...`). The two do not match → validation fails.
2. Receipts to CGM were 503 `not_configured`. Gateway was hard-coding `PROVIDER_SERVICE_URL` + `BOOTSTRAP_SU_API_KEY` with `X-Api-Key` header. CGM reads `GATEWAY_SERVICE_KEY` via `X-Service-Key`, and `GATEWAY_SERVICE_KEY` was empty in prod.
3. Client body was bloated — CGM was expected to pass `patient_guid`, `contract_guid`, `concept_guid` per observation, duplicating state gateway already has.

### What changed in gateway.pdhc

- `gateway_app/app/services/pat_validation.py` — `PATValidationResult` now carries `push_endpoint_url` + `push_secret` fields sourced from request.pdhc `/provider/validate-token` (which in turn reads them from the PAT record). Cache round-trip persists/restores them so both cache-hit and cache-miss paths populate `g.pat_result.push_endpoint_url/push_secret`.
- `gateway_app/app/services/report_ingestion.py`:
  - Enrichment loop now **overwrites** `obs.concept_guid` with `txn.goal_concept_guid` (measurement concept), stashing the original procedure concept on `obs.procedure_concept_guid` for audit. This is the SCOPE_VIOLATION root-cause fix.
  - SR context is fetched **before** grant validation. `patient_guid` is derived from `sr_context.patient_guid`, so the client no longer has to send it. A client-supplied `patient_guid` is still accepted as a cross-check and rejected with `PATIENT_MISMATCH` if it disagrees.
  - `_send_receipt` reads `push_endpoint_url`/`push_secret` from `g.pat_result` instead of global config → per-PAT routing.
- `gateway_app/app/services/push_service.py` — `send_receipt_to_provider(push_endpoint_url, push_secret, receipt_data)`. Derives the receipts URL from `push_endpoint_url` by swapping `/inbound/push → /receipts/ingest` (first wrong-derivation attempt produced `/api/v1/inbound/receipts/ingest`; fixed by stripping the full `/inbound/push` suffix). Sends `X-Service-Key`.

### Minimal client body (now supported)

```json
{
  "status": "in-progress",
  "grant_token": "...",
  "report_payload": {
    "observations": [
      {"transaction_guid": "...", "value": 6.2, "recorded_at": "2026-04-11T10:50:00Z"}
    ]
  }
}
```

Gateway derives `patient_guid`, `contract_guid`, `concept_guid`, `unit`, `response_type`, `range_*`, etc. from sr_context + transaction enrichment.

### Deploy

- scp'd `pat_validation.py`, `report_ingestion.py`, `push_service.py` to `/usr/local/www/gateway.pdhc/gateway_app/app/services/` on miserver.
- `./safe_restart.sh` — current gunicorn master pid **80953**.
- request.pdhc side (context_service.py goal_*fallback + provider.py /validate-token push field) deployed via `docker-compose up -d --build app` on that service.
- cgm.pdhc side (receipts.py fallback accepting `PUSH_SECRET`) deployed via scp + manual gunicorn TERM/relaunch (old master stuck in HUP respawn loop). Fresh master pid **80568**.

### Verification — full flow for SR `523d1227-132b-4d2a-8129-fdbb1519b039`

- POST to `https://gateway.pdhc.se/api/v1/provider/report/523d1227-...` with minimal body → **HTTP 202**.
- gateway_pdhc_db.inbound_observations row:
  - `concept_guid = 1c34a590-fc2d-430c-92e6-d123b95fe392` (B-glucos — **measurement, not procedure**)
  - `resolution_status = resolved`
- cgm_prod_db.gateway_receipts row:
  - `receipt_guid = 31c6ccb5-0c8e-4e1e-a3d6-bb65377a0875`
  - `observations_stored = 1`
- audit_log cutover: CGM POSTs flipped from `403 GRANT_TOKEN_INVALID` (10:50 UTC) → `202 OK` (10:51 UTC) after the grant_service.py hotfix from earlier in the session.

### 2026-04-11 afternoon — second round: persistent 422 on SR 78528324

User showed a CGM dashboard screenshot with three batches in Observation Log all marked "Failed". Investigation:

- Gateway access log confirmed CGM's streaming worker was POSTing every ~60s to `/api/v1/provider/report/78528324-...` and getting **422 VALIDATION_ERROR** on every attempt.
- Replaying the POST manually with the real `transaction_guid` (`27e61cc8-95b5-41b5-ab8f-21ec47285c52`) from the SR snapshot returned **202**. So gateway was fine — the CGM body was the problem.
- Root cause: `cgm_portal/app/services/acknowledgement.py :: _resolve_transaction_guid()` walks `careplan_json['activity'][]['detail']['extension'][]['_pdhc_transactions']` looking for a transaction_guid, and falls back to the literal string `'tx-glucose'` if that path isn't populated. Dumped the cached CarePlan for this task — it's a standard FHIR R5 CarePlan with `activity[].detail.code.coding[]` carrying the concept code but **no `_pdhc_transactions` extension anywhere**. So `_resolve_transaction_guid` always returns `'tx-glucose'`, the streamer sends that as the transaction_guid, gateway's enrichment loop finds no match in `txn_map`, leaves `concept_guid` and `response_type` unset, and the validator fires on both missing fields → 422.

### Fix — single-transaction fallback

Immediate unblock: added a single-transaction fallback in the enrichment loop that mirrors the single-goal fallback already in `request.pdhc/context_service._extract_transactions`. If `txn_map` has exactly one entry and the client's `transaction_guid` doesn't resolve, pick the single transaction and overwrite `obs.transaction_guid` with the real guid. Safe because:

- Most CGM-style SRs today have exactly one transaction.
- Gateway is already the authority for concept/unit/range fields — inferring the one and only transaction does not weaken any access control.
- For multi-transaction SRs the fallback is skipped (still 422), so providers aren't silently masked.

Deployed via `safe_restart.sh` — new gunicorn master **pid 83861**.

### Verification — full flow for SR `78528324-2ffd-4b7f-8499-942ddec9aa03`

| check | result |
|---|---|
| Manual replay with bogus `tx-glucose` id | HTTP **202** — `receipt_guid 7400fd4e-...` |
| gateway_pdhc_db.inbound_observations row (replay) | `transaction_guid=27e61cc8-... concept_guid=1c34a590-... (B-glucos) resolution_status=resolved` |
| Live CGM streaming batch at 12:18:01 UTC | stored with same enrichment — `concept_guid=1c34a590-...`, receipt `20ade390-...` delivered back to CGM |
| cgm_prod_db.gateway_receipts at 12:18:01 | accepted, `observations_stored=1` |

### Follow-up (not done today)

Long-term, gateway should surface transaction_guids in the CarePlan bundle that providers receive — likely a `_pdhc_transactions` FHIR extension on each `activity.detail` — and teach CGM's `_resolve_transaction_guid` to read it. That's the right answer for multi-transaction plans (diet + glucose + weight, etc.) where single-transaction inference can't save us. Tracked as an open design question, not a blocker for today's demo.

---

## 2026-04-19 — Ticket #90: late-arrival flag on inbound reports

Paired change with `request.pdhc`. See `request.pdhc/progress.md` for
the feed/download half.

### Changes on gateway.pdhc

- `app/services/sr_context.py` — `SRContextResult.period_end` returns a
  parsed, timezone-aware `datetime` (or None). Request.pdhc was already
  returning `period_end` in its `/internal/service-request/<guid>/context`
  response; the gateway side just didn't surface it.
- `app/models/inbound_observation.py` — new `is_late` boolean column
  (default False, indexed). Exposed in `to_dict()`.
- `migrations/versions/d1e2f3a4b5c6_add_inbound_observation_is_late.py`
  — adds the column via `batch_alter_table` (SQLite-safe).
- `app/services/report_ingestion.py` — `ingest()` computes
  `is_late = now > sr.period_end` once (None period = never late) and
  stamps every stored `InboundObservation` (including QuestionnaireResponse
  parent + child rows). Audit `payload_snapshot` and the provider receipt
  both carry the flag. Archived SRs are NOT gated — reports flow through
  the normal chain, just flagged.
- Tests: `tests/test_late_flag.py` (4/4) covers late / on-time /
  open-ended / archived-is-still-accepted paths; `tests/test_sr_context.py`
  gains two cases for period_end parsing.

### Test suite
- New: 12/12 pass (4 late-flag, 2 period_end, 6 prior SR context).
- Full suite: 240 pass, 8 fail — all 8 failures are pre-existing and
  unrelated to ticket #90:
  - 3 `test_push_service.TestReceiptPush.*` — stale test signatures
    (tests call `send_receipt_to_provider(url, dict)` but the actual
    method signature is `(url, secret, receipt_data)`).
  - 1 `test_report_submission.TestContractScope.test_concept_not_in_scope`
    — test overrides `concept_guid` in the body but gateway overwrites
    `concept_guid` from the SR context (intentional anti-forgery), so
    the test no longer exercises what it claims.
  - 1 `test_observations_page.TestObservationDetail.test_detail_shows_fhir_json`
    — template text no longer contains literal "FHIR Observation JSON".
  - 3 `test_upstream_services.*` — smoke tests against live upstreams
    that aren't running in this env.
- Confirmed pre-existing by re-running these tests with ticket #90's
  edits not yet applied; same failures.

### Pre-deploy plan (as of 2026-04-19T15:37Z)
**Not deployed.** Requires migration `d1e2f3a4b5c6` before the app image
is rebuilt — order is:
1. `scp` the source across (see changed_files.md for list).
2. `docker-compose up -d --no-deps --build app` on miserver:/usr/local/www/gateway.pdhc/current.
3. `docker exec <app container> flask db upgrade` to run the migration.
4. Probe `/healthz` and submit a test report against an SR with
   `period_end` in the past; expect `is_late: true` in the accepted
   response.

Waiting for operator greenlight. Per §14 of root CLAUDE.md, deploys
(especially migrations) stay explicit.

> **Note**: Phase A pre-flight (2026-04-19T18:34Z) invalidated step 2
> above — gateway.pdhc runs bare-metal gunicorn, not Docker. See the
> Deploy status block below for the actual path taken.

### Deploy status
**Deployed to macmini 2026-04-19T18:42Z.** Note: gateway.pdhc runs
**bare-metal gunicorn**, not Docker — only `gateway_pdhc_db` is
containerised. App lives at `/usr/local/www/gateway.pdhc.se/gateway_app/`
(`.se` suffix), pidfile at `/usr/local/www/gateway.pdhc.se/gunicorn.pid`,
bind `127.0.0.1:9050`. Original deploy plan (docker-compose rebuild)
was wrong — revised to scp + venv `flask db upgrade` + `safe_restart.sh`.

Shipped (sha-verified): `sr_context.py`, `inbound_observation.py`,
`report_ingestion.py`, `migrations/versions/d1e2f3a4b5c6_…py`.
Alembic head advanced `c85ba6a368c2 → d1e2f3a4b5c6`. `\d
inbound_observations` confirms `is_late boolean NOT NULL DEFAULT false`
column + `ix_inbound_observations_is_late` index. All 6278 existing
rows defaulted to `is_late=false` (safe backfill, no false positives).

`safe_restart.sh` completed cleanly; new PID 55063. `/api/v1/health`
returns 200 `{"status":"ok","database":"connected"}` on both
internal and external. Pre-backup at
`~/backups/20260419T183459Z_ticket90_preflight/gateway_pdhc_db.pgdump`
(1.3M).

End-to-end late-flag test against prod data: no existing SRs have
`period_end` set, so the `is_late=true` branch will only be exercised
on new SRs created after ticket #90's request.pdhc-side UI surfaces
the field. Schema + code paths verified; runtime-behaviour verification
deferred to first real late submission.

---

## Ticket #93 — 10-min idle auto-logout did not work on gateway.pdhc

### Root cause (2026-04-20T19:08Z)

`app/services/sso_service.py::get_access_blob()` returned
`session.get('access_blob')` directly — the access blob was cached in
the Flask session cookie and never re-checked with SSO. Other pdhc
services (request.pdhc, plan.pdhc, dashboard.pdhc) call
`validate_sso_token(session['sso_token'])` on every call via the
ticket #50 pattern; that's what makes SSO-side idle expiry (10 min)
visible downstream. gateway.pdhc missed that patch.

Secondary: no `PERMANENT_SESSION_LIFETIME` set → Flask's 31-day default.

### Fix

- `gateway_app/app/services/sso_service.py` — rewrote `get_access_blob`
  to re-validate against SSO every call. Added `_clear_sso_session`
  helper that pops `sso_token`, `access_blob`, `role` on rejection.
- `gateway_app/config.py` — added
  `PERMANENT_SESSION_LIFETIME = timedelta(hours=8)` so the local cookie
  can't outlive its SSO counterpart.

### Deploy (2026-04-20T19:08–19:10Z)

Server backups created:
- `…/app/services/sso_service.py.bak.20260420T190832Z`
- `…/config.py.bak.20260420T190832Z`

scp'd both files; sha256 verified local == server. Ran
`safe_restart.sh`: graceful stop → pip install -q → flask db upgrade
(no-op, no migration) → gunicorn daemon on 127.0.0.1:9050. Health OK
on first attempt, new PID 40485.

Smoke: `GET /` anonymous → 302 auth/login (unchanged). `GET
/observations` anonymous → 302 auth/login (unchanged). `GET
/api/v1/health` → 200 `{"status":"ok","database":"connected"}`.

Real idle-timeout verification requires an authenticated browser
session idle for 10+ min — deferred to operator check.

### Side effect

Each authenticated gateway.pdhc request now makes one HTTP call to
`sso.pdhc/api/auth/me/service`. Same overhead request.pdhc already
accepted with ticket #50. Latency impact: ~20–50ms per request on the
LAN, negligible.
