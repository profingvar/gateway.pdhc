# cdr1 single-source-of-truth cutover plan

**Status:** draft  
**Author:** Claude (Opus 4.7), 2026-06-27  
**Owner:** ingvar  
**Estimated duration:** 4–6 weeks elapsed, ~3–5 working days across phases  
**Tickets:** one per phase (see §10).

## 1. Goal

Today gateway forwards every concept-resolved observation to cdr1 (live
since 2026-06-27, see commits `216a0ac` + `2787b22`), but **both
sides keep a full copy**. This plan removes gateway's copy after
successful forward so cdr1 becomes the platform's true single source
of truth — matching the architectural intent stated in CLAUDE.md and
on pdhc.se.

This is not a one-shot change. It cuts five existing readers off
gateway and reroutes them, plus adjusts dedup, validation, and admin
UI. Six sequenced phases, each independently shippable.

## 2. What changes for the platform

**Before this cutover:**
- gateway: `inbound_observations` (all rows, forever) +
  `cdr_delivery_log` (status tracking).
- cdr1: `ingest_raw` + `fhir_resources` (full copy).
- Reads come from gateway (analyse-pull, admin UI, receipt service).
- Dedup is on gateway via `inbound_observations.payload_hash`.

**After this cutover:**
- gateway: only `cdr_delivery_log`, extended with the dedup keys
  needed to short-circuit re-POSTs. No row-level observation data.
- cdr1: `ingest_raw` + `fhir_resources` + `observation` (the canonical
  layer, populated by cdr1's own pipeline) — sole source.
- Reads come from cdr1.
- Dedup uses `cdr_delivery_log.payload_hash` + cdr1's
  `DedupeRegistry` (defence in depth).

## 3. Readers that must be rerouted

Inventoried 2026-06-27 by grep over `gateway_app/app/`:

| Caller | Today | After cutover |
|---|---|---|
| `app/api/observations.py` (`GET /api/v1/observations`) — analyse-pull for dashboards | Queries `InboundObservation` directly + `_to_fhir_observation` builder | Proxy to cdr1, OR remove and have dashboard.pdhc call cdr1 directly |
| `app/web/views.py` — admin pages `/observations`, `/observations/<guid>` | `InboundObservation.query` + template `observations_list.html`, `observation_detail.html` | Remove from gateway. If admin UI is still needed, build it in cdr.pdhc. |
| `app/services/receipt_service.py:26` — provider ACK lookup | `InboundObservation.query.filter_by(receipt_guid=...)` | Lookup via cdr1's `ingest_raw.source_system_id` (= our inbound guid) — needs new cdr1 endpoint, or store enough in `cdr_delivery_log` |
| `app/services/contract_scope.py:180` | Cross-references `InboundObservation` (verify exactly what) | Verify in phase 1 whether this can be eliminated or rerouted |
| `app/services/report_ingestion.py:299,339,476` — **dedup** | `InboundObservation.query.filter_by(service_request_guid, payload_hash)` | `CdrDeliveryLog.query.filter_by(payload_hash, ...)` — phase 1 |
| `app/services/cdr_forwarder.py:101` | Reads InboundObservation to build payload | Reads from same table as long as it exists; after phase 5 inserts deletion |

## 4. Schema changes

### Phase 1 (`cdr_delivery_log`)

Add nullable columns (backfilled from `inbound_observations` for
existing rows):
- `payload_hash` String(64) indexed
- `dedup_key` String(64) indexed
- `service_request_guid` String(36) indexed
- `received_at` DateTime(tz)
- `concept_guid` String(36) — useful for cdr1 lookup later

Relax the `inbound_observation_guid` FK to nullable + `ON DELETE SET
NULL` so the log row outlives the source row after phase 5.

### Phase 5 (`inbound_observations`)

No schema change yet — just deletion of rows whose
`cdr_delivery_log.status='delivered'`.

### Phase 6 (drop unused)

After all readers have moved (phase 2–4 done), drop the
`inbound_observations` table entirely OR strip its columns down to a
minimal receipt stub. Decision deferred to phase 6.

## 5. Phase 1 — dedup-on-log + schema (foundation)

**No external impact.** All existing readers keep working.

Steps:
1. Alembic migration `add_cdr_delivery_log_dedup_columns`:
   - Add 5 nullable columns (above).
   - Backfill from `inbound_observations` in a single
     `UPDATE … FROM` (~7064 rows, < 1s).
   - Drop & re-add FK with `ondelete='SET NULL'` + make
     `inbound_observation_guid` nullable.
   - Add unique partial index on `(service_request_guid, payload_hash)`
     where `payload_hash IS NOT NULL` — the dedup key.
2. Update `report_ingestion.py:299` (main dedup) +
   lines 339 + 476 (re-POST + freeform branches): change the query
   from `InboundObservation` to `CdrDeliveryLog`. Keep the
   `InboundObservation` insert as-is for now (still the source of
   truth until phase 5).
3. Update the hook in `report_ingestion.py` (the `db.session.add(CdrDeliveryLog(...))`
   calls added in commit `2787b22`) to populate the new columns at
   insertion time.
4. New tests: dedup hits a `CdrDeliveryLog` row after the inbound row
   is gone (simulate by deleting the inbound row manually in the test).
5. Inspect `contract_scope.py:180` — answer: is this a reader of
   inbound data, or a counter/aggregate? Document the finding in the
   ticket; if it's a real reader, treat as part of phase 3.

**Acceptance:**
- All existing tests pass.
- New test: re-POST of an observation whose inbound row was deleted
  still returns "already received" (dedup match on log).
- `cdr_delivery_log.payload_hash` populated for all 7064 historical
  rows.

**Ticket:** #281 (placeholder — see §10)

## 6. Phase 2 — receipt-service reroute

Provider POSTs an observation → gateway returns a `receipt_guid` →
provider later calls `POST /api/v1/provider/receipt/{receipt_guid}/ack`.
The receipt is the inbound row's guid today.

Steps:
1. cdr1: add `GET /api/v1/ingest/by-source-id/<source_system_id>` —
   look up by the gateway-side guid we stored as
   `ingest_raw.source_system_id`. Auth: same X-Source-Service header
   gateway already uses.
2. gateway `receipt_service.py:26`: drop the
   `InboundObservation.query.filter_by(receipt_guid=...)` and replace
   with HTTP call to cdr1's new endpoint. Fall back to gateway's
   `cdr_delivery_log` for status if cdr1 lookup fails (safety net
   while we're cutting over).
3. Tests: receipt-ack flow end-to-end (provider POSTs, gateway
   forwards, cdr1 stores, provider acks → gateway responds OK without
   touching `inbound_observations`).

**Acceptance:**
- Receipt-ack works without any gateway read of `inbound_observations`.
- Latency budget: gateway → cdr1 round-trip < 100 ms p95
  (host.docker.internal hop is fast; we can probe).

**Ticket:** #282

## 7. Phase 3 — analyse-pull endpoint reroute

**Highest external risk.** `GET /api/v1/observations?organization=...`
is the analyse-pull endpoint that dashboard.pdhc and other consumers
use. It returns a FHIR R5 searchset Bundle of Observations.

Two options:

**Option A — proxy:** keep the gateway endpoint URL, internally call
cdr1's equivalent. Pros: zero change for consumers. Cons: gateway
stays in the read path and the bundle assembly still touches the
existing builder.

**Option B — redirect/decommission:** dashboard.pdhc and other
consumers are updated to call cdr1 directly. Gateway endpoint returns
410 Gone after the transition.

Phase 3 picks **Option A** initially (proxy) because it's reversible
and doesn't require updating consumers. Option B becomes a separate
optional cleanup.

Steps:
1. cdr1: add or verify `GET /api/v1/observations?organization=<guid>`
   returning the same FHIR R5 searchset Bundle shape. Today the
   builder logic lives in `gateway.pdhc/app/services/fhir_observation_builder.py`
   — extract it into a shared schema if needed, or duplicate
   judiciously.
2. gateway `app/api/observations.py`: replace the query+build logic
   with an HTTP call to cdr1's endpoint, forwarding the SSO bearer
   token and org filter. Keep the auth + phase-gate logic on the
   gateway side (SSO has analysis-access decision).
3. Audit: gateway still writes the `observation.read` audit event
   (the platform's read-side audit), even though the data came from
   cdr1.
4. Tests: smoke against `dashboard.pdhc` smoke + a couple of curl
   probes.

**Acceptance:**
- dashboard.pdhc loads observations as before; same JSON shape; no
  consumer changes.
- p95 latency < 500 ms (workflow priority budget per §7 of
  procurement plan).
- Audit events still recorded.

**Ticket:** #283

## 8. Phase 4 — admin UI

Gateway's `/observations` and `/observations/<guid>` (admin web pages
in `app/web/views.py`) — these are the easiest to handle because they
are internal-only and have no external consumers.

Decision: **remove from gateway**. If an admin observation viewer is
needed, build it in `cdr.pdhc` instead (separate ticket).

Steps:
1. Delete views + templates in gateway.
2. Add a note in `gateway.pdhc/readme.md` pointing to where the admin
   observation viewer will live (TBD).
3. Update nav in `base.html` if there's a link.

**Acceptance:**
- Routes return 404. No broken nav. SSO callback chain intact.

**Ticket:** #284

## 9. Phase 5 — activate deletion

The actual SSOT switch. Only safe after phases 1–4 are complete.

Steps:
1. Add config `CDR_FORWARDING_DELETE_AFTER_DELIVERY=false` default.
2. In `cdr_forwarder._deliver_one`, after marking
   `log.status='delivered'`: if the new flag is true, also
   `db.session.delete(obs_row)` (the InboundObservation row).
3. CLI: `flask delete-already-delivered` — one-shot to clean up the
   7060 historical rows now in `cdr_delivery_log.status='delivered'`
   but still present in `inbound_observations`. Chunked, with
   `--dry-run` first.
4. Deploy + run dry-run first. Verify counts.
5. Run for real, verify `inbound_observations` rowcount drops.
6. Flip `CDR_FORWARDING_DELETE_AFTER_DELIVERY=true` for forward
   deletion.

**Acceptance:**
- `cdr_delivery_log` has correct counts (no orphans, all
  `inbound_observation_guid` set to NULL after cleanup).
- New POST → row exists for ≤ 60 s in gateway, then deleted after
  forward; receipt-ack still works (because it queries cdr1 now).
- Dedup still works.

**Ticket:** #285

## 10. Phase 6 — schema cleanup

After phase 5 has run for ≥ 1 week without issue, decide whether to:
- Drop `inbound_observations` table entirely (provider receipts then
  live in `cdr_delivery_log` exclusively), OR
- Strip `inbound_observations` columns down to just guid +
  patient_guid + payload_hash as a minimal "we saw this" stub.

Recommendation: **drop the table.** `cdr_delivery_log` already has
everything we need by then.

Steps:
1. Alembic migration: drop the FK from `validation_log` (if it still
   references `inbound_observations`), then drop the table.
2. Remove `app/models/inbound_observation.py`, model registry entry,
   and any remaining imports.
3. Update `CapabilityStatement` in `app/__init__.py` to reflect that
   gateway no longer surfaces Observation reads.

**Acceptance:**
- All tests pass.
- `flask db upgrade` to head completes.
- Nothing references the dropped model.

**Ticket:** #286

## 11. Cross-cutting risks

| Risk | Mitigation |
|---|---|
| Dashboard.pdhc breaks during phase 3 cutover | Option A proxy (no consumer change); roll back by toggling proxy off |
| Re-POST dedup fails after phase 5 | Phase 1 adds dedup on `cdr_delivery_log` before phase 5 enables deletion — order matters |
| FK constraint broken on phase 5 | Phase 1 migrates FK to `ON DELETE SET NULL`; no cascade surprises |
| `validation_log` FK breaks | Phase 6 drops it; phase 5 needs nothing because we only delete rows, not the table |
| cdr1 outage during forward | Existing forwarder retry budget (5 attempts × backoff ≈ 5 min) covers; `flask recover-failed-cdr` for longer outages |
| Provider POSTs flood during phase 5 cleanup | Deletion respects `cdr_delivery_log.status='delivered'`; new pending rows are untouched |
| `urn:pdhc:concept` ≠ LOINC means cdr1.observation stays empty (known issue from initial deploy) | Out of scope for this plan; tracked separately; doesn't block SSOT cutover because `fhir_resources` + `ingest_raw` are sufficient for analyse-read |

## 12. Out of scope (deferred)

- Switching the shared docker network (gateway and cdr1 still talk
  via `host.docker.internal:9046`). Better, but doesn't block SSOT.
- Rotating `GATEWAY_PDHC_SERVICE_KEY` (currently
  `dev-gateway-key-change-me`). Should happen before real patient
  data, but not coupled to SSOT.
- Fixing cdr1's `_extract_loinc` to read `urn:pdhc:concept` (the
  observation-table empty issue).
- Building an admin observation viewer in cdr.pdhc.

## 13. Tickets (in dependency order)

Created on ticket.mitidbok.se on 2026-06-27:

| # | Phase | Title | Blocks |
|---|---|---|---|
| #280 | 1 | dedup-on-log + schema foundation | #281 #282 #284 |
| #281 | 2 | receipt-service reroute → cdr1 | #284 |
| #282 | 3 | analyse-pull reroute → cdr1 (Option A proxy) | #284 |
| #283 | 4 | remove admin /observations UI | — |
| #284 | 5 | activate deletion (CDR_FORWARDING_DELETE_AFTER_DELIVERY) | #285 |
| #285 | 6 | drop inbound_observations table | — |

Recommended sequence:
- **#280 first** (foundation, no external impact).
- Then **#281 #282 #283 in parallel** (independent of each other).
- Then **#284** when all of #280/#281/#282/#283 are merged + deployed.
- Then **#285** after #284 has been running stable for ≥ 1 week.
