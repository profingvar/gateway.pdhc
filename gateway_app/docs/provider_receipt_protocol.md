# Provider Receipt Protocol

Instructions for recoding provider.pdhc to accept receipts from gateway.pdhc.

## Overview

When gateway.pdhc accepts observation data from a provider, it sends a receipt back to provider.pdhc confirming the data was received and stored. This gives providers an audit trail of their submissions.

## Receipt format

Gateway sends `POST /api/v1/receipts/ingest` to provider.pdhc with:

```json
{
  "receipt_guid": "uuid",
  "service_request_guid": "uuid",
  "patient_guid": "uuid",
  "provider_org_guid": "uuid",
  "contract_guid": "uuid",
  "observations_stored": 3,
  "accepted_at": "2026-03-26T07:00:00Z",
  "payload_hash": "sha256-hex"
}
```

## Authentication

Gateway authenticates to provider.pdhc using `X-Api-Key` header with a shared service key.

## What provider.pdhc needs to implement

### 1. New model: `InboundReceipt`

```python
class InboundReceipt(db.Model):
    __tablename__ = 'inbound_receipts'

    id = db.Column(db.Integer, primary_key=True)
    guid = db.Column(db.String(36), unique=True)
    receipt_guid = db.Column(db.String(36), unique=True, index=True)
    service_request_guid = db.Column(db.String(36), index=True)
    patient_guid = db.Column(db.String(36), index=True)
    provider_org_guid = db.Column(db.String(36), index=True)
    contract_guid = db.Column(db.String(36))
    observations_stored = db.Column(db.Integer)
    accepted_at = db.Column(db.DateTime(timezone=True))
    payload_hash = db.Column(db.String(64))
    received_at = db.Column(db.DateTime(timezone=True))
```

### 2. New endpoint: `POST /api/v1/receipts/ingest`

```python
@api_bp.route('/receipts/ingest', methods=['POST'])
def ingest_receipt():
    """Accept a receipt from gateway.pdhc."""
    api_key = request.headers.get('X-Api-Key')
    if not api_key or api_key != current_app.config.get('GATEWAY_SERVICE_KEY'):
        return jsonify({'code': 'UNAUTHORIZED'}), 401

    body = request.get_json()
    receipt = InboundReceipt(
        receipt_guid=body['receipt_guid'],
        service_request_guid=body['service_request_guid'],
        patient_guid=body['patient_guid'],
        provider_org_guid=body['provider_org_guid'],
        contract_guid=body['contract_guid'],
        observations_stored=body.get('observations_stored', 0),
        accepted_at=body.get('accepted_at'),
        payload_hash=body.get('payload_hash'),
    )
    db.session.add(receipt)
    db.session.commit()
    return jsonify({'status': 'accepted', 'receipt_guid': receipt.receipt_guid}), 201
```

### 3. Dashboard display

Add a "Receipts" column or badge to the provider dashboard showing:
- Whether a receipt has been received for each submitted report
- Link to receipt detail (accepted_at, observations_stored, payload_hash)

### 4. Configuration

Add to provider.pdhc `.env`:
```bash
GATEWAY_SERVICE_KEY=<shared-key-with-gateway>
```

## Delivery semantics

- Fire-and-forget: gateway does not block on receipt delivery
- If provider.pdhc is unreachable, the receipt is logged but not retried
- Receipts are idempotent: duplicate receipt_guid is ignored
