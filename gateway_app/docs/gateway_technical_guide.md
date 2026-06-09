# Gateway.pdhc — Technical Documentation

## Architecture

Flask application with PostgreSQL (pgvector extension) running in Docker. Part of the PDHC microservice platform.

**Ports**: 9050 (Flask/gunicorn), 9051 (PostgreSQL)

## Database models

| Table | Purpose |
|-------|---------|
| `inbound_observations` | Stores received observation data with composite key fields |
| `observation_vectors` | Resolved GUID chain context stored as vectors (experimental) |
| `guid_resolution_cache` | Cache for upstream GUID lookups with TTL |
| `validation_log` | Per-observation validation results |
| `audit_log` | GDPR-compliant audit trail |

## Services

| Service | Purpose |
|---------|---------|
| `PATValidationService` | Validates provider tokens via request.pdhc upstream |
| `GrantValidationService` | HMAC-SHA256 composite key validation |
| `ObservationValidator` | FHIR R5 observation schema validation |
| `ReportIngestionService` | Full validation chain + storage |
| `GuidResolutionService` | Resolves transaction → careplan → plandefinition chain |
| `VectorService` | Builds and stores resolved context vectors |
| `FeedService` | Proxies provider feed/download from request.pdhc |
| `PushService` | Push delivery to providers + receipt sending |
| `ReceiptService` | Receipt acknowledgement |

## Upstream dependencies

| Service | Port | Purpose |
|---------|------|---------|
| request.pdhc | 9060 | PAT validation, GUID chain resolution, feed/download proxy |
| plan.pdhc | 9030 | PlanDefinition repository (via request.pdhc proxy) |
| provider.pdhc | 9070 | Receipt delivery target |

## Configuration (.env)

```bash
DATABASE_URL=postgresql://gateway_user:password@localhost:9051/gateway_db
FLASK_SECRET_KEY=<random-64-char>
HMAC_SECRET=<random-64-char-separate-from-flask-secret>
REQUEST_SERVICE_URL=https://request.pdhc.se/api/v1
PROVIDER_SERVICE_URL=http://localhost:9070/api/v1
GUID_CACHE_TTL_SECONDS=3600
BOOTSTRAP_SU_API_KEY=<initial-superuser-key>
PUSH_TIMEOUT_SECONDS=30
PUSH_RETRY_COUNT=3
PGVECTOR_DIMENSIONS=384
```

## Admin pages (SSO, admin-only)

| Route | Purpose |
|-------|---------|
| `/admin/cache` | Cache stats by type (total/fresh/stale), per-type and global flush buttons, recent entries |
| `/admin/cache/flush` | POST — flush `mode=stale` or `mode=all`, optional `source_type` filter |
| `/admin/health-report` | Live probes of upstream services (request.pdhc, contract.pdhc, sso.pdhc, local DB) + error events from last 24h |
| `/pats` | PAT validation/rejection activity per provider org |
| `/audit` | Paginated GDPR audit trail |
| `/grants` | DataExchangeGrant validity tracking |

### Cache flush details

The `guid_resolution_cache` table stores upstream responses keyed by `(source_guid, source_type)` with a per-entry TTL. Flush options:

- **Flush stale**: iterates entries and deletes those where `is_expired()` is true (age > ttl_seconds). No disruption — these would be re-fetched on next access anyway.
- **Flush all**: deletes all entries (optionally filtered by source_type). Causes a burst of upstream calls as the cache repopulates.

Neither flush requires a service restart. The cache is in Postgres, not in-process memory.

## Running

```bash
# Development
./start.sh

# Production restart
./safe_restart.sh
```

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
| `/provider/report/<sr_guid>` | POST | Inbound (not a read) | per-row inside ReportIngestionService | `report.accepted` / `report.rejected` | Write path. Not in this ticket's scope but noted for completeness. |

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
