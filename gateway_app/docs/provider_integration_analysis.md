# Provider.pdhc Integration Analysis (Tillägg 5)

Analysis of `../provider.pdhc` and what needs to be done for automatic, documented data transfer.

## Current Architecture

```
request.pdhc ──(push)──→ provider.pdhc (POST /api/v1/inbound/push)
                              │
                              ▼ (user completes task)
                          ProviderTask
                              │
                              ▼ (submits report)
                         upstream_client.py
                              │
                    (PAT + composite key)
                              │
                              ▼
                  request.pdhc (POST /provider/report/<sr>)
```

Provider.pdhc currently sends reports to **request.pdhc**, not directly to gateway.pdhc. The gateway.pdhc receives data that has already passed through request.pdhc's validation.

## What provider.pdhc already has

| Component | File | Status |
|-----------|------|--------|
| Upstream client (PAT-based) | `services/upstream_client.py` | Working |
| Feed polling | `services/subscription.py` | Working (needs PAT) |
| Bundle download | `upstream_client.py:50-61` | Working |
| Report submission | `upstream_client.py:63-85` | Working |
| Receipt acknowledgement | `upstream_client.py:87-92` | Working |
| Inbound push receiver | `api/inbound.py` | Working (needs PUSH_SECRET) |
| Background sync scheduler | `services/sync_scheduler.py` | Disabled by default |
| Guided response builder | `services/guided_response.py` | Working |

## Configuration gaps (must be set for automation)

### 1. PROVIDER_TOKEN (critical)
```bash
# In provider.pdhc/.env — currently empty
PROVIDER_TOKEN=<PAT issued by request.pdhc admin>
```
Without this, poll-based feed sync cannot authenticate.

### 2. PUSH_SECRET (critical)
```bash
# In provider.pdhc/.env — not present
PUSH_SECRET=<shared secret with request.pdhc>
```
Without this, inbound push endpoint returns 503.

### 3. SYNC_ENABLED (for automation)
```bash
# In provider.pdhc/.env — currently false
SYNC_ENABLED=true
SYNC_INTERVAL_SECONDS=60
```
Set to `true` to enable automatic polling every 60 seconds.

## Changes needed in provider.pdhc for full automation

### Change 1: Add receipt ingestion endpoint

Provider.pdhc needs to accept receipts from gateway.pdhc (tilläggsuppdrag 1). See `provider_receipt_protocol.md` for the full specification.

**New endpoint:** `POST /api/v1/receipts/ingest`
- Accepts receipts for both accepted and rejected submissions
- Stores in new `InboundReceipt` model
- Authenticated via `X-Api-Key` (shared service key with gateway)

### Change 2: Add GATEWAY_SERVICE_KEY to .env

```bash
GATEWAY_SERVICE_KEY=<shared key with gateway.pdhc>
```

### Change 3: Silent error handling in report submission

**File:** `services/report_submission.py` lines 64-71
**Problem:** If upstream push fails, exception is caught with `pass` — no logging, no retry.
**Fix:** Add logging and retry logic:
```python
except Exception as e:
    logger.error('Report push failed for %s: %s', receipt_token, e)
    # Mark for retry instead of silently ignoring
    task.push_status = 'failed'
    db.session.commit()
```

### Change 4: Store submitted payload in SubmissionReceipt

**File:** `models/submission_receipt.py`
**Problem:** Only stores `payload_hash`, not the actual payload.
**Fix:** Add `provider_payload` JSON column for audit trail compliance.

### Change 5: Display receipts from gateway in dashboard

Add a receipts view showing:
- Which submissions were accepted/rejected by gateway
- Rejection reasons
- Timestamp and payload hash for reconciliation

## Data flow after all changes

```
request.pdhc ──(push)──→ provider.pdhc
                              │
                              ▼ (complete task)
                         upstream_client.py
                              │
              (PAT + composite key + observations)
                              │
                              ▼
                  gateway.pdhc (POST /provider/report/<sr>)
                              │
                    ┌─────────┴─────────┐
                    │                   │
               accepted             rejected
                    │                   │
                    ▼                   ▼
              receipt sent        receipt sent
             (accepted=true)    (accepted=false)
                    │                   │
                    ▼                   ▼
          provider.pdhc (POST /api/v1/receipts/ingest)
                              │
                              ▼
                    receipt stored + displayed
```

## Checklist for enabling automation

- [ ] Set `PROVIDER_TOKEN` in provider.pdhc `.env`
- [ ] Set `PUSH_SECRET` in provider.pdhc `.env`
- [ ] Set `SYNC_ENABLED=true` in provider.pdhc `.env`
- [ ] Add `GATEWAY_SERVICE_KEY` to provider.pdhc `.env`
- [ ] Implement receipt ingestion endpoint in provider.pdhc
- [ ] Fix silent error handling in report_submission.py
- [ ] Add payload storage to SubmissionReceipt model
- [ ] Set `PROVIDER_SERVICE_URL` in gateway.pdhc `.env`
- [ ] Ensure request.pdhc has issued a PAT for this provider
- [ ] Ensure HMAC_SECRET is shared between request.pdhc and gateway.pdhc
