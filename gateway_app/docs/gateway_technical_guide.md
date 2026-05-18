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
