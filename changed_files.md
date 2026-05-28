# gateway.pdhc — Changed Files

All edited files with full path, per Rule 17.

---

| File | Change | Date |
|------|--------|------|
| `./gateway_app/app/api/observations.py` | Route clinical Quantity unit refs through plan.pdhc per platform principle (only plan.pdhc emits external code-system refs). `valueQuantity` and `referenceRange.low/high` now use `system="https://plan.pdhc.se/api/v1/lookup/units"` (with the UCUM-compatible `unit_name` as `code`) instead of `http://unitsofmeasure.org`. Standard FHIR-meta refs unchanged (Observation category, security profiles). | 2026-05-18 |
| `./gateway_app/app/services/sso_service.py` | Ticket #93: re-validate SSO token on every `get_access_blob()` call so 10-min idle timeout takes effect (ticket #50 pattern) | 2026-04-20 |
| `./gateway_app/config.py` | Ticket #93: add `PERMANENT_SESSION_LIFETIME = 8h` (was relying on Flask 31-day default) | 2026-04-20 |
| `./gateway_app/app/services/sr_context.py` | Ticket #90: expose `period_end` as parsed aware datetime for late-arrival detection | 2026-04-19 |
| `./gateway_app/app/models/inbound_observation.py` | Ticket #90: add `is_late` boolean column (indexed) | 2026-04-19 |
| `./gateway_app/migrations/versions/d1e2f3a4b5c6_add_inbound_observation_is_late.py` | Ticket #90: migration for `is_late` column | 2026-04-19 |
| `./gateway_app/app/services/report_ingestion.py` | Ticket #90: compute and stamp `is_late` on observations; audit + receipt carry flag | 2026-04-19 |
| `./gateway_app/tests/test_late_flag.py` | Ticket #90: new coverage for late / on-time / open-ended / archived ingestion paths | 2026-04-19 |
| `./gateway_app/tests/test_sr_context.py` | Ticket #90: added `period_end` parsing tests | 2026-04-19 |
| `./readme.md` | Created — deployment plan | 2026-03-26 |
| `./progress.md` | Created — progress tracking | 2026-03-26 |
| `./changed_files.md` | Created — file change log | 2026-03-26 |
| `./CLAUDE.md` | Created — design system reference | 2026-03-26 |
| `./start.sh` | Created — startup script (ports 9050–9053) | 2026-03-26 |
| `./gateway_app/requirements.txt` | Created — Python dependencies | 2026-03-26 |
| `./gateway_app/config.py` | Created — Flask config with HMAC, vector, upstream settings | 2026-03-26 |
| `./gateway_app/Dockerfile` | Created — Python 3.12-slim, port 9050 | 2026-03-26 |
| `./gateway_app/docker-compose.yml` | Created — pgvector:pg16 + Flask | 2026-03-26 |
| `./gateway_app/.env` | Created — environment variables | 2026-03-26 |
| `./gateway_app/.gitignore` | Created — ignore venv, pycache, .env | 2026-03-26 |
| `./gateway_app/app/__init__.py` | Created — Flask app factory | 2026-03-26 |
| `./gateway_app/app/extensions.py` | Created — SQLAlchemy + Migrate | 2026-03-26 |
| `./gateway_app/app/errors.py` | Created — error handlers | 2026-03-26 |
| `./gateway_app/app/api/__init__.py` | Created — API blueprint | 2026-03-26 |
| `./gateway_app/app/api/provider.py` | Created — stub provider endpoints | 2026-03-26 |
| `./gateway_app/app/web/__init__.py` | Created — web blueprint | 2026-03-26 |
| `./gateway_app/app/web/views.py` | Created — dashboard view | 2026-03-26 |
| `./gateway_app/app/models/__init__.py` | Created — model imports | 2026-03-26 |
| `./gateway_app/app/models/inbound_observation.py` | Created — observation model | 2026-03-26 |
| `./gateway_app/app/models/observation_vector.py` | Created — vector model (experimental) | 2026-03-26 |
| `./gateway_app/app/models/guid_resolution_cache.py` | Created — GUID resolution cache | 2026-03-26 |
| `./gateway_app/app/models/validation_log.py` | Created — validation log model | 2026-03-26 |
| `./gateway_app/app/models/audit_log.py` | Created — audit log model | 2026-03-26 |
| `./gateway_app/app/services/__init__.py` | Created — services placeholder | 2026-03-26 |
| `./gateway_app/templates/base.html` | Created — base template (extends pdhc.css) | 2026-03-26 |
| `./gateway_app/templates/dashboard.html` | Created — dashboard template | 2026-03-26 |
| `./gateway_app/templates/404.html` | Created — 404 template | 2026-03-26 |
| `./gateway_app/static/css/pdhc.css` | Copied from ../css_instrux/pdhc.css | 2026-03-26 |
| `./gateway_app/tests/__init__.py` | Created — test package | 2026-03-26 |
| `./gateway_app/tests/conftest.py` | Created — pytest fixtures | 2026-03-26 |
| `./gateway_app/tests/test_health.py` | Created — Phase 1 tests (8 tests) | 2026-03-26 |
| `./gateway_app/app/services/pat_validation.py` | Created — PAT validation via upstream + cache | 2026-03-26 |
| `./gateway_app/app/services/grant_validation.py` | Created — HMAC composite key validation | 2026-03-26 |
| `./gateway_app/app/api/auth.py` | Created — @require_provider_token decorator | 2026-03-26 |
| `./gateway_app/app/services/__init__.py` | Updated — export PAT + Grant services | 2026-03-26 |
| `./gateway_app/app/models/guid_resolution_cache.py` | Fixed — timezone-aware is_expired() | 2026-03-26 |
| `./gateway_app/tests/test_pat_validation.py` | Created — PAT validation tests (10 tests) | 2026-03-26 |
| `./gateway_app/tests/test_grant_validation.py` | Created — grant validation tests (11 tests) | 2026-03-26 |
| `./gateway_app/app/services/observation_validator.py` | Created — FHIR R5 observation validation | 2026-03-26 |
| `./gateway_app/app/services/report_ingestion.py` | Created — full validation chain + storage | 2026-03-26 |
| `./gateway_app/app/services/receipt_service.py` | Created — receipt acknowledgement | 2026-03-26 |
| `./gateway_app/app/api/provider.py` | Updated — wired real endpoints with auth | 2026-03-26 |
| `./gateway_app/app/services/__init__.py` | Updated — export all Phase 3 services | 2026-03-26 |
| `./gateway_app/tests/test_report_submission.py` | Created — Phase 3 tests (20 tests) | 2026-03-26 |
| `./gateway_app/tests/test_health.py` | Updated — endpoints now return 401 (auth enforced) | 2026-03-26 |
| `./gateway_app/tests/test_pat_validation.py` | Updated — adjusted for auth enforcement | 2026-03-26 |
| `./gateway_app/tests/test_upstream_services.py` | Created — integration probe tests (5 tests) | 2026-03-26 |
| `./gateway_app/pytest.ini` | Created — integration marker registration | 2026-03-26 |
| `./readme.md` | Updated — receipt protocol (3.d), data flow diagram | 2026-03-26 |
| `./gateway_app/app/services/guid_resolution.py` | Created — GUID chain resolution service | 2026-03-26 |
| `./gateway_app/app/services/vector_service.py` | Created — vector construction + query service | 2026-03-26 |
| `./gateway_app/app/api/vectors.py` | Created — vector query endpoints | 2026-03-26 |
| `./gateway_app/app/api/__init__.py` | Updated — register vectors blueprint | 2026-03-26 |
| `./gateway_app/app/services/__init__.py` | Updated — export Phase 4 services | 2026-03-26 |
| `./gateway_app/tests/test_guid_resolution.py` | Created — Phase 4 tests (20 tests) | 2026-03-26 |
| `./gateway_app/app/web/views.py` | Updated — observations list + detail views | 2026-03-26 |
| `./gateway_app/templates/observations_list.html` | Created — observations list page | 2026-03-26 |
| `./gateway_app/templates/observation_detail.html` | Created — observation detail page | 2026-03-26 |
| `./gateway_app/templates/base.html` | Updated — Observations nav link | 2026-03-26 |
| `./gateway_app/templates/dashboard.html` | Updated — phase status + dash-grid links | 2026-03-26 |
| `./gateway_app/tests/test_observations_page.py` | Created — page tests (10 tests) | 2026-03-26 |
| `./gateway_app/app/services/feed_service.py` | Created — feed/download proxy service | 2026-03-26 |
| `./gateway_app/app/api/provider.py` | Updated — wired feed + download endpoints | 2026-03-26 |
| `./gateway_app/app/api/auth.py` | Updated — store raw_token on g | 2026-03-26 |
| `./gateway_app/app/services/__init__.py` | Updated — export FeedService | 2026-03-26 |
| `./gateway_app/tests/test_feed_service.py` | Created — Phase 5 tests (10 tests) | 2026-03-26 |
| `./gateway_app/tests/test_report_submission.py` | Updated — feed stub → proxy test | 2026-03-26 |
| `./gateway_app/app/services/push_service.py` | Created — push delivery + receipt push | 2026-03-26 |
| `./gateway_app/app/services/__init__.py` | Updated — export PushService | 2026-03-26 |
| `./gateway_app/tests/test_push_service.py` | Created — Phase 6 tests (12 tests) | 2026-03-26 |
| `./gateway_app/tests/test_hardening.py` | Created — Phase 7 tests (17 tests) | 2026-03-26 |
| `./gateway_app/tests/test_all_endpoints.py` | Created — Phase 8 endpoint tests (21 tests) | 2026-03-26 |
| `./gateway_app/docs/provider_receipt_protocol.md` | Created — receipt protocol + provider recoding instructions | 2026-03-26 |
| `./safe_restart.sh` | Created — graceful restart for web deployment | 2026-03-26 |
| `./gateway_app/app/services/report_ingestion.py` | Updated — rejection receipts (tillägg 1) | 2026-03-26 |
| `./gateway_app/config.py` | Updated — PROVIDER_SERVICE_URL config | 2026-03-26 |
| `./gateway_app/docs/fhir_data_format.md` | Created — FHIR format alignment (tillägg 3) | 2026-03-26 |
| `./gateway_app/docs/gateway_user_guide.md` | Created — non-technical guide (tillägg 4) | 2026-03-26 |
| `./gateway_app/docs/gateway_technical_guide.md` | Created — technical documentation (tillägg 4) | 2026-03-26 |
| `./gateway_app/docs/api_documentation.md` | Created — API reference (tillägg 4) | 2026-03-26 |
| `./gateway_app/docs/authentication_guide.md` | Created — auth procedure guide (tillägg 4) | 2026-03-26 |
| `./gateway_app/app/web/docs.py` | Created — docs download routes (tillägg 4) | 2026-03-26 |
| `./gateway_app/templates/docs_index.html` | Created — docs listing page (tillägg 4) | 2026-03-26 |
| `./gateway_app/app/web/__init__.py` | Updated — import docs module | 2026-03-26 |
| `./gateway_app/templates/base.html` | Updated — Docs nav link | 2026-03-26 |
| `./gateway_app/docs/provider_integration_analysis.md` | Created — provider automation analysis (tillägg 5) | 2026-03-26 |
| `./gateway_app/docs/fhir_r5_compliance_verification.md` | Created — FHIR R5 compliance audit (tillägg 6) | 2026-03-26 |
| `./gateway_app/app/models/service_request_status.py` | Created — SR completion tracking model (tillägg 7) | 2026-03-26 |
| `./gateway_app/app/models/__init__.py` | Updated — export ServiceRequestStatus | 2026-03-26 |
| `./gateway_app/app/services/request_completion.py` | Created — completion/expiry tracking service (tillägg 7) | 2026-03-26 |
| `./gateway_app/app/services/__init__.py` | Updated — export RequestCompletionService | 2026-03-26 |
| `./gateway_app/app/services/report_ingestion.py` | Updated — call _track_delivery after storage (tillägg 7) | 2026-03-26 |
| `./gateway_app/app/web/views.py` | Updated — requests_list view (tillägg 7) | 2026-03-26 |
| `./gateway_app/templates/requests_list.html` | Created — SR status listing page (tillägg 7) | 2026-03-26 |
| `./gateway_app/templates/base.html` | Updated — Requests nav link | 2026-03-26 |
| `./gateway_app/templates/dashboard.html` | Updated — Requests dashboard card | 2026-03-26 |
| `./gateway_app/tests/test_request_completion.py` | Created — tillägg 7 tests (12 tests) | 2026-03-26 |
| `./gateway_app/docs/gateway_theoretical_paper.md` | Created — academic paper on gateway semantics | 2026-03-26 |
| `./gateway_app/app/services/pat_validation.py` | Updated — PATValidationResult carries push_endpoint_url + push_secret from request.pdhc /validate-token; cached and rehydrated | 2026-04-11 |
| `./gateway_app/app/services/report_ingestion.py` | Updated — (1) enrichment stamps obs.concept_guid from txn.goal_concept_guid (B-glucos) instead of transaction's procedure concept (CGM); (2) SR context fetched BEFORE grant validation so client body no longer needs patient_guid/contract_guid; (3) _send_receipt reads g.pat_result.push_endpoint_url/push_secret for per-PAT routing | 2026-04-11 |
| `./gateway_app/app/services/push_service.py` | Updated — send_receipt_to_provider takes explicit url+secret, derives receipts URL from push_endpoint_url (/inbound/push → /receipts/ingest), sends X-Service-Key | 2026-04-11 |
| `./gateway_app/app/services/report_ingestion.py` | Updated — single-transaction fallback in enrichment. If client-supplied `transaction_guid` is missing or not in `txn_map` AND the SR has exactly one transaction, infer it and rewrite `obs.transaction_guid` to the real one before enrichment/validation. Root cause: CGM's `_resolve_transaction_guid` falls back to the string `'tx-glucose'` because the FHIR CarePlan bundle it receives doesn't expose transaction_guids — those live in the SR snapshot, not the bundle. Deployed via safe_restart.sh (master pid 83861). TODO: long-term fix is to surface transaction_guids in the careplan bundle via a `_pdhc_transactions` extension, then teach `_resolve_transaction_guid` to read it. For multi-transaction SRs the single-txn fallback won't help and the provider must send the real guid. | 2026-04-11 |
| `./gateway_app/app/web/views.py` | Updated — added `/admin/cache` (cache stats + flush stale/all), `/admin/cache/flush` (POST), `/admin/health-report` (upstream service probes + recent error events). All admin-only, SSO-protected. | 2026-04-12 |
| `./gateway_app/templates/cache_management.html` | Created — cache management page: entries by type, fresh/stale counts, per-type and global flush buttons, recent entries table. | 2026-04-12 |
| `./gateway_app/templates/health_report.html` | Created — health report page: upstream service probes (gateway DB, request.pdhc, contract.pdhc, sso.pdhc), latency, DB status, recent error events (pat.rejected, report.rejected, bundle.push_failed) from last 24h. | 2026-04-12 |
| `./gateway_app/templates/base.html` | Updated — added Cache and Health nav links in admin section. | 2026-04-12 |
| `./gateway_app/templates/dashboard.html` | Updated — added Cache and Health Report dashboard cards. | 2026-04-12 |
- 2026-04-16: gateway_app/app/__init__.py — ticket #70 adds CORS on /api/v1/health (Access-Control-Allow-Origin https://www.pdhc.se + Methods GET + Vary: Origin + Cache-Control: no-store). Note: local was stale (server had _register_metadata + _register_stockholm_filter); synced server→local before edit.
- 2026-05-28: gateway_app/app/services/contract_scope.py + gateway_app/app/services/report_ingestion.py + gateway_app/tests/test_contract_scope.py — ticket #147 (Phase G #9). validate_observations() now accepts service_request_guid; on status='completed' it unions concept_guids from the current batch with InboundObservation rows for the same SR (concept_guid IS NOT NULL, DISTINCT), so an obligatory satisfied in an earlier in-progress submission no longer needs to be re-supplied in the closing batch. report_ingestion passes the SR guid into the call. 4 new tests in TestObligatoryAcrossPriorSubmissions; all 16 contract_scope tests pass. Deployed via docker compose up -d --build app (tests/ excluded from image via .dockerignore; runtime code verified in container).
