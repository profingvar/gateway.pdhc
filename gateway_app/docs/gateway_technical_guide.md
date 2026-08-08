# Gateway.pdhc — Technical Documentation

## Architecture

Flask application with PostgreSQL (pgvector image) running in Docker as part of the PDHC microservice platform. The whole service is containerised: `gateway_pdhc_app` (Flask/gunicorn) and `gateway_pdhc_db` (Postgres) are two containers in the same compose project.

The gateway is a validating relay, not a datastore. Inbound provider reports are validated, queued for delivery, and forwarded to the clinical data repository (cdr1), which is the single source of truth for observation data. The gateway keeps only delivery/dedup bookkeeping, per-service-request status, an upstream-lookup cache, and the audit trail. The former local observation tables (`inbound_observations`, `observation_vectors`) have been dropped.

**Ports**: 9050 (Flask/gunicorn, bound to 127.0.0.1), 9051 (PostgreSQL, bound to 127.0.0.1)

## Database models

| Table | Model | Purpose |
|-------|-------|---------|
| `guid_resolution_cache` | `GuidResolutionCache` | Cache for upstream GUID/context lookups, keyed by `(source_guid, source_type)` with a per-entry TTL |
| `service_request_status` | `ServiceRequestStatus` | Per-SR completion/expiry tracking (active / completed / expired / partial), delivery counters, grant expiry |
| `cdr_delivery_log` | `CdrDeliveryLog` | The forwarder queue. One row per accepted observation; holds the FHIR resource, denormalised dedup keys, retry/status bookkeeping, and the captured operator session id |
| `audit_log` | `AuditLog` | GDPR-compliant audit trail |

Dropped tables (do not reference — they no longer exist):

- `inbound_observations` — dropped in migration `d7e8f9a0b1c2`. The gateway no longer stores observations locally; `cdr_delivery_log` is the sole queue + dedup index + provenance source, and cdr1 is the permanent store.
- `validation_log` — dropped in migration `d7e8f9a0b1c2`.
- `observation_vectors` — dropped in migration `f3a4b5c6d7e8`.

### `cdr_delivery_log` (forwarder queue)

Written directly by `ReportIngestionService` (there is no intermediate `InboundObservation` row). Each row carries `patient_guid`, `service_request_guid`, `concept_guid`, `transaction_guid`, `contract_guid`, `provider_org_guid`, `fhir_observation_json`, dedup columns (`payload_hash`, `dedup_key`), `operator_session_id` (X2 #408), and delivery state (`status`, `attempt_count`, `last_attempt_at`, `last_error`, `delivered_at`, `cdr_resource_id`).

Row status:
- `pending` — a complete production observation, ready to forward. Requires `service_request_guid`, `concept_guid`, `contract_guid`, `provider_org_guid` (enforced at both the ingest boundary and in the forwarder; a `pending` row with any of these NULL is marked terminally failed rather than shipped dirty).
- `skipped` — QR parent rows and freeform/unresolved-concept rows that are not forwardable. Never sent to cdr1.
- `delivered` / `failed` — terminal states set by the forwarder.

## Services

| Service | Purpose |
|---------|---------|
| `PATValidationService` | Validates provider tokens via request.pdhc upstream |
| `GrantValidationService` | Validates the composite key by **delegating** to request.pdhc `/internal/grant/validate` — the HMAC secret never leaves request.pdhc (see below) |
| `ObservationValidator` | FHIR R5 observation schema validation |
| `ReportIngestionService` | Full validation chain + write to `cdr_delivery_log` + receipt push |
| `GuidResolutionService` | Resolves transaction → careplan → plandefinition chain (cached) |
| `SRContextService` / `ContractScopeService` | Fetch SR context and contract return-scope from upstream (cached) |
| `RequestCompletionService` | Maintains `service_request_status` (delivery progress, expiry) |
| `FeedService` | Proxies provider feed/download from request.pdhc |
| `PushService` / `ReceiptService` | Per-PAT receipt push to providers + acknowledgement |
| `fhir_observation_builder` | Builds the fully referenced FHIR R5 Observation + the canonical typed observation core (#500) |
| `CdrClient` + `cdr_forwarder` worker | Forwards `cdr_delivery_log` rows to cdr1 (`/api/v1/ingest`) |
| `AnalyseClient` | Analyse-pull proxy client → analyse.pdhc `/api/v1/observations` |

`GrantValidationService` no longer computes HMAC locally. It POSTs `{sr_guid, patient_guid, org_guid, grant_token}` with `X-Service-Key: REQUEST_INTERNAL_SERVICE_KEY` to `REQUEST_SERVICE_URL/internal/grant/validate` and returns request.pdhc's verdict (with `contract_guid`, `grant_type`, `uses_remaining` on success). The old local `VectorService` has been removed.

## The gateway → cdr1 forwarder

Accepted observations are inserted into `cdr_delivery_log` with `status='pending'` (insert-then-send). A background APScheduler job, `run_forwarding_cycle`, drains the queue:

- Started in `create_app` (`_start_cdr_forwarder_scheduler`), gated on `CDR_FORWARDING_ENABLED` so a first deploy can be dark. Interval is `CDR_FORWARDING_INTERVAL_SECONDS` (default 60s).
- Both gunicorn workers start a scheduler; the worker claims rows with `SELECT ... FOR UPDATE SKIP LOCKED` (Postgres) so the two schedulers never double-process. The SQLite test path falls back to a plain `SELECT` (single-process).
- Per-row exponential backoff (`BASE_BACKOFF=10s`, `MAX_ATTEMPTS=5`, `BATCH_LIMIT=50`). A 5xx / network error is retryable; a terminal 4xx from cdr1 marks the row `failed` without burning the retry budget.
- Delivery goes through `CdrClient.deliver_one` → `POST {CDR_BASE_URL}/api/v1/ingest` with headers `X-Source-Service: gateway.pdhc`, `X-Service-Key: GATEWAY_PDHC_SERVICE_KEY`, `X-Request-Id`, and `X-Operator-Session-Id` (replayed from the row so the chain-of-custody survives the async gap).
- The payload includes the FHIR R5 resource, provenance re-hydrated from the SR context (best-effort — a context-fetch failure does not block delivery), and the **canonical typed observation** (`build_canonical_observation`, #500). The canonical core is also carried inside the FHIR resource as extension `urn:pdhc:fhir:extension:canonical-observation`, so the openEHR projection (rosetta, #501) reads real typed values rather than the lossy FHIR `value[x]`.

`CDR_FORWARDING_DELETE_AFTER_DELIVERY` is now dead config (there is no local observation row left to delete) and is ignored.

## The analyse-pull proxy (`GET /api/v1/observations`)

`GET /api/v1/observations?organization=<org_guid>` is an SSO-gated JSON proxy for analysis-phase consumers — it is **not** a local observation-browsing page (the gateway stores no observations). Gateway responsibilities on this route:

- validate the SSO bearer token and require analysis-phase access (403 otherwise),
- org-membership check; admin cross-org read requires an `X-Admin-Justification` header (#220),
- resolve `SR → contract → requesting_org` locally (gateway owns this mapping; contract.pdhc is its upstream) and compute the matching SR list,
- proxy the pre-computed SR filter to the analyse layer, which federates over CDR1–6 and returns one merged FHIR R5 searchset Bundle,
- apply the IPS spärr filter to drop blocked provider rows (#206 / PDL Ch 4 §4),
- audit the read (`observations.read` / `observations.admin_read`).

The proxy target is `ANALYSE_BASE_URL`, which now points at **analyse.pdhc** (`http://host.docker.internal:9110`). This replaced the earlier dashboard.pdhc target — the code default was repointed to analyse (#540) and the server `.env` follows. `AnalyseClient` authenticates with `X-Source-Service: gateway.pdhc` + `X-Service-Key: GATEWAY_PDHC_SERVICE_KEY` (the same key value gateway uses for cdr1) plus the outbound operator-session headers.

## Upstream / downstream dependencies

| Service | Purpose | Config |
|---------|---------|--------|
| request.pdhc | PAT validation, grant validation, GUID chain, SR context, feed/download proxy | `REQUEST_SERVICE_URL`, `REQUEST_INTERNAL_SERVICE_KEY` |
| contract.pdhc | Contract scope / parties validation | `CONTRACT_SERVICE_URL`, `CONTRACT_INTERNAL_SERVICE_KEY` |
| sso.pdhc | Bearer-token validation for analysis endpoints + operator login | `SSO_BASE_URL`, `SSO_CLIENT_ID`, `SSO_CLIENT_SECRET`, `SSO_CALLBACK_URL` |
| cdr.pdhc (cdr1) | Forwarding target — permanent observation store | `CDR_BASE_URL`, `GATEWAY_PDHC_SERVICE_KEY` |
| analyse.pdhc | Analyse-pull federation target | `ANALYSE_BASE_URL`, `GATEWAY_PDHC_SERVICE_KEY` |
| provider (per-PAT) | Receipt delivery — routed per-PAT via `push_endpoint_url` / `push_secret` from request.pdhc, not a fixed URL |

## Configuration (.env)

```bash
# Database (Postgres container gateway_pdhc_db)
DATABASE_URL=postgresql://gateway_pdhc:password@db:5432/gateway_pdhc_db
FLASK_SECRET_KEY=<random-64-char>

# request.pdhc — PAT + grant validation, GUID chain, SR context
REQUEST_SERVICE_URL=https://request.pdhc.se/api/v1
REQUEST_INTERNAL_SERVICE_KEY=<shared key with request.pdhc /internal/*>
GUID_CACHE_TTL_SECONDS=3600
GRANT_CACHE_TTL_SECONDS=60

# contract.pdhc — contract scope validation
CONTRACT_SERVICE_URL=https://contract.pdhc.se
CONTRACT_INTERNAL_SERVICE_KEY=<shared key with contract.pdhc /internal/*>

# SSO (analysis endpoints + operator login)
SSO_BASE_URL=https://sso.pdhc.se
SSO_CLIENT_ID=<gateway sso client id>
SSO_CLIENT_SECRET=<gateway sso client secret>
SSO_CALLBACK_URL=https://gateway.pdhc.se/auth/callback
# AUTH_DISABLED=true   # local dev only — never set on the server

# cdr1 forwarding
CDR_BASE_URL=http://cdr_pdhc_app:9046
CDR_FORWARDING_ENABLED=true
CDR_FORWARDING_INTERVAL_SECONDS=60
CDR_TIMEOUT_SECONDS=30

# Shared service key: cdr1 ingest AND analyse-pull both authenticate with it.
# Must match GATEWAY_PDHC_SERVICE_KEY in cdr.pdhc and analyse.pdhc .envs.
GATEWAY_PDHC_SERVICE_KEY=<shared service key>

# Analyse-pull federation target (analyse.pdhc, #540)
ANALYSE_BASE_URL=http://host.docker.internal:9110

# Bootstrap + receipt push
BOOTSTRAP_SU_API_KEY=<initial-superuser-key>
PUSH_TIMEOUT_SECONDS=30
PUSH_RETRY_COUNT=3
```

Notes:
- There is **no** `HMAC_SECRET` — grant validation is delegated to request.pdhc, so gateway holds no HMAC secret.
- The DB user/name are `gateway_pdhc` / `gateway_pdhc_db` (compose defaults).
- Receipt delivery has no fixed `PROVIDER_SERVICE_URL`; each PAT carries its own `push_endpoint_url` / `push_secret`.

## Admin pages (SSO, admin-only)

| Route | Purpose |
|-------|---------|
| `/admin/cache` | Cache stats by source type (total/fresh/stale), per-type and global flush buttons, recent entries |
| `/admin/cache/flush` | POST — flush `mode=stale` or `mode=all`, optional `source_type` filter |
| `/admin/health-report` | Live probes of the gateway DB, request.pdhc, contract.pdhc, sso.pdhc + error events from last 24h |
| `/pats` | PAT validation/rejection activity per provider org |
| `/audit` | Paginated GDPR audit trail |
| `/grants` | Grant/`service_request_status` validity tracking (active / expiring / expired) |

### Cache flush details

The `guid_resolution_cache` table stores upstream responses keyed by `(source_guid, source_type)` with a per-entry TTL. Flush options:

- **Flush stale**: iterates entries and deletes those where `is_expired()` is true (age > `ttl_seconds`). No disruption — these would be re-fetched on next access anyway.
- **Flush all**: deletes all entries (optionally filtered by `source_type`). Causes a burst of upstream calls as the cache repopulates.

Neither flush requires a service restart. The cache is in Postgres, not in-process memory.

## Health endpoint

`GET /api/v1/health` (and `/metadata` for the FHIR CapabilityStatement) is public/anonymous — it pings the DB and returns the canonical shape:

```json
{"status": "ok", "service": "gateway.pdhc", "database": "connected"}
```

HTTP 200 when the DB is reachable, 503 when `degraded`. It sets `Access-Control-Allow-Origin: https://www.pdhc.se` so services.html can read the real status/DB fields cross-origin (CLAUDE.md §10).

## Running

The service is containerised — there is no `start.sh` / `safe_restart.sh`. Deploy and restart via docker compose from the service directory on the macmini:

```bash
# Build + (re)start both containers, picking up code and .env changes
docker compose up -d --build

# Apply migrations against the running app container
docker exec gateway_pdhc_app flask db upgrade

# Tail logs
docker compose logs -f app
```

`docker compose up -d --build` is required after code changes: the image bakes source via `COPY . .`, so scp'ing files + a bare `restart` runs stale code. `docker restart` also keeps create-time env — use `docker compose up -d` to pick up host `.env` changes. Never `docker compose down -v` (deletes the data volume) and never touch Colima or sibling services.

## Testing

```bash
cd gateway_app
source venv/bin/activate

# Unit tests (excludes integration)
pytest tests/ -m "not integration"

# All tests including live service probes
pytest tests/ -m integration

# Full suite
pytest tests/ -v
```

## Read-side audit granularity (ticket #221, PDL Ch 4 § 3)

A read returning rows about N patients can audit either *per-query*
(one row per call, cheap) or *per-patient* (one row per patient
touched, richer for kontroller — "did anyone read patient P's data").
PDL Ch 4 § 3 obliges every vårdgivare to be able to answer that
kontroller question. The right granularity per route is a deliberate
trade between answerability and audit-table volume.

### Decision matrix

| Route | Method | Patient identifiers in response? | Granularity | `event_type` | Rationale |
|---|---|---|---|---|---|
| `/observations` (normal scope) | GET | Yes — FHIR Bundle with patient refs | **per-query** with `patient_guids[]` in payload | `observations.read` | High-volume analyse-phase polling. The per-query row carries the full sorted patient_guids list, so kontroller can answer "was P in any read by X?" with `WHERE payload_snapshot->'patient_guids' ? '<guid>'`. Storing the list once per query is ~36 bytes × n_patients vs. one full row per patient. |
| `/observations` (admin off-org bypass) | GET | Yes | **per-patient** | `observations.admin_read` | Rare and high-stakes (PDL Ch 4 § 1 — admins are not exempt from need-to-know without an audit trail). One row per patient touched, each carrying the same justification text + correlation_id, so kontroller filters cheaply by patient. Volume cost is acceptable because this path is rare; the X-Admin-Justification gate (#220) caps frequency. |
| `/provider/feed` | GET | Metadata only (SR refs, no PHI) | **per-query** | `provider.feed.polled` | Polled on a 30s cadence. PDL § 3 cares about reads of patient *data*; metadata polling that yields no PHI is the wrong granularity for per-patient rows. Snapshot carries `since` cursor + `n_items` + `limit`, enough to reconstruct what window the provider polled. |
| `/provider/download/<sr_guid>` | GET | Yes — full Bundle for a single SR | per-query (which IS per-patient: one SR == one patient) | `bundle.downloaded` | The route is single-SR. Per-query and per-patient collapse to the same row. No change needed. |
| `/provider/report/<sr_guid>` | POST | Inbound (not a read) | per-row inside ReportIngestionService | `report.received` / `report.rejected` | Write path. Not in this ticket's scope but noted for completeness. |

### When to use which

- **Per-query** is the right shape when (a) the result set is
  predictable from query args alone, and (b) the audit consumer can
  answer "did P appear?" from a list in the snapshot. Cheaper writes.
- **Per-patient** is the right shape when (a) the act of reading is
  high-stakes per patient (admin bypass, indispensable-care lifts,
  cross-caregiver reads), or (b) the result set is sparse and adding
  the patient list to the snapshot would be more data than one row
  per patient.
- **Neither** is right for routes that don't touch patient-identified
  data (capability statements, metadata, healthchecks) — those skip
  audit rows entirely.

### Per-patient kontroller query (PG)

For the `observations.read` normal path (per-query with patient_guids
list), an operator can answer "show every read of patient P by any
user" with:

```sql
SELECT *
FROM audit_log
WHERE event_type = 'observations.read'
  AND payload_snapshot->'patient_guids' ? '<patient-guid>'
ORDER BY created_at DESC;
```

For the admin-read path (per-patient rows), the equivalent is the
direct filter:

```sql
SELECT *
FROM audit_log
WHERE event_type = 'observations.admin_read'
  AND payload_snapshot->>'patient_guid' = '<patient-guid>'
ORDER BY created_at DESC;
```

Both surface the same answer; the granularity choice changes only
which side pays the storage cost (writer-side row explosion vs.
reader-side JSONB index lookup).
