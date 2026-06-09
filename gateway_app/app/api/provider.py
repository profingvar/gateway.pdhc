"""Provider-facing API endpoints.

These are the endpoints that provider.pdhc calls:
- POST /provider/report/<sr_guid>  — submit observation report
- GET  /provider/feed              — poll for ServiceRequests
- GET  /provider/download/<sr_guid> — download FHIR Bundle
- POST /provider/receipt/<token>/ack — acknowledge delivery
"""
from flask import request, jsonify, g
from . import api_bp
from .auth import require_provider_token
from ..services.report_ingestion import ReportIngestionService
from ..services.receipt_service import ReceiptService
from ..services.feed_service import FeedService
from ..errors import APIError


@api_bp.route('/provider/report/<service_request_guid>', methods=['POST'])
@require_provider_token(scope='write')
def submit_report(service_request_guid):
    """Receive an observation report from a provider.

    Validation chain (Phase G of the provider integration guide):
    1. PAT validated (by decorator) → 401 UNAUTHORIZED
    2. PAT has 'write' scope (by decorator) → 403 FORBIDDEN
    3. PAT org matches body.organisation_guid (cross-check) → 403
    4. Grant token HMAC matches → 403 GRANT_TOKEN_INVALID
    5. Grant token not expired → 403 GRANT_EXPIRED
    6. body.patient_guid matches SR.patient_guid → 403 PATIENT_MISMATCH
    7. Contract still active → 403 CONTRACT_INACTIVE
    8. All concepts within contract.return_scope → 403 SCOPE_VIOLATION
    9. On completed: every obligatory concept present → 422 VALIDATION_ERROR

    Error envelope: {error, code, message, service_request_guid, [details]}.
    """
    g.service_request_guid = service_request_guid
    body = request.get_json()
    if not body:
        raise APIError('JSON body required', code='BAD_REQUEST', status_code=400)

    result = ReportIngestionService.ingest(service_request_guid, body)
    return jsonify(result), 202


@api_bp.route('/provider/receipt/<receipt_token>/ack', methods=['POST'])
@require_provider_token(scope='write')
def ack_receipt(receipt_token):
    """Acknowledge a delivery receipt."""
    result = ReceiptService.acknowledge(receipt_token, g.provider_org_guid)
    return jsonify(result), 200


@api_bp.route('/provider/feed', methods=['GET'])
@require_provider_token(scope='read')
def provider_feed():
    """List ServiceRequests for this provider (metadata only).

    Proxies from request.pdhc. Supports query params:
    - since: ISO-8601 datetime
    - limit: max results
    - cursor: pagination cursor

    Audit granularity (ticket #221): **per-query**. The feed returns
    metadata only — no patient observation data — and providers poll
    on a 30s cadence. Per-patient audit rows on every poll would
    multiply the audit table 30x without adding kontroller value
    (PDL Ch 4 § 3 cares about reads of patient *data*; metadata
    polling that yields no PHI is the wrong granularity for
    per-patient rows). One row per call carrying the `since` cursor
    and the result count is enough for kontroller to reconstruct
    which feeds the provider polled.
    """
    data, status = FeedService.get_feed(g.raw_token)
    if 200 <= status < 300:
        try:
            from ..models import AuditLog
            from ..extensions import db
            n_items = None
            if isinstance(data, dict):
                entries = data.get('entry') or data.get('items') or []
                if isinstance(entries, list):
                    n_items = len(entries)
            audit = AuditLog(
                event_type='provider.feed.polled',
                actor_guid=g.provider_org_guid,
                receipt_token=None,
                ip_address=request.remote_addr,
                correlation_id=request.headers.get('X-Correlation-Id'),
                payload_snapshot={
                    'provider_org_guid': g.provider_org_guid,
                    'since': request.args.get('since'),
                    'limit': request.args.get('limit'),
                    'cursor': request.args.get('cursor'),
                    'n_items': n_items,
                    'granularity': 'per-query',
                },
            )
            db.session.add(audit)
            db.session.commit()
        except Exception:
            db.session.rollback()
    return jsonify(data), status


@api_bp.route('/provider/download/<service_request_guid>', methods=['GET'])
@require_provider_token(scope='read')
def download_bundle(service_request_guid):
    """Download full FHIR Bundle + grant_token for a ServiceRequest.

    Proxies from request.pdhc. Audit-logged per the provider integration
    guide (ticket #137).
    """
    g.service_request_guid = service_request_guid
    data, status = FeedService.download_bundle(service_request_guid, g.raw_token)
    if 200 <= status < 300:
        try:
            from ..models import AuditLog
            from ..extensions import db
            audit = AuditLog(
                event_type='bundle.downloaded',
                actor_guid=g.provider_org_guid,
                receipt_token=service_request_guid,
                ip_address=request.remote_addr,
                correlation_id=request.headers.get('X-Correlation-Id'),
                payload_snapshot={
                    'service_request_guid': service_request_guid,
                    'provider_org_guid': g.provider_org_guid,
                },
            )
            db.session.add(audit)
            db.session.commit()
        except Exception:
            db.session.rollback()
    return jsonify(data), status
