"""Receipt acknowledgement service.

Handles provider acknowledgement of delivery receipts. Looks up the
receipt's existence via two paths (SSOT phase 2 / ticket #281):

  1. Local CdrDeliveryLog (carries service_request_guid since #280) —
     fast, in-process. Hits when receipt_token is a service-request
     GUID, which is the current production pattern.
  2. Fallback to cdr.pdhc /api/v1/ingest/by-source-id/<token> —
     covers the case where receipt_token is an inbound-observation
     GUID and the local row has already been deleted (phase 5).

Either hit sets has_local_record=True in the audit. Both misses set
False but the ack still succeeds (acknowledgement is unconditional —
the lookup is advisory).
"""
import logging
import requests
from flask import current_app, request as flask_request
from ..models import CdrDeliveryLog, AuditLog
from ..extensions import db

logger = logging.getLogger(__name__)


class ReceiptService:

    @staticmethod
    def acknowledge(receipt_token, provider_org_guid):
        """Acknowledge a delivery receipt.

        Args:
            receipt_token: the receipt/service_request GUID
            provider_org_guid: the authenticated provider org

        Returns:
            dict with ack status
        """
        has_local_record, lookup_source = _lookup(receipt_token)

        audit = AuditLog(
            event_type='receipt.acknowledged',
            actor_guid=provider_org_guid,
            resource_guid=receipt_token,
            ip_address=flask_request.remote_addr,
            correlation_id=flask_request.headers.get('X-Correlation-Id'),
            payload_snapshot={
                'receipt_token': receipt_token,
                'has_local_record': has_local_record,
                'lookup_source': lookup_source,
            },
        )
        db.session.add(audit)
        db.session.commit()

        return {
            'status': 'acknowledged',
            'receipt_token': receipt_token,
        }


def _lookup(receipt_token):
    """Two-stage lookup. Returns (has_record: bool, source: str).

    source ∈ {'local', 'cdr1', 'none'}.
    """
    # 1. Local CdrDeliveryLog — receipt_token == service_request_guid
    # is the current production pattern (current route name is
    # /provider/receipt/<receipt_token>/ack and the token is the SR
    # guid in every observed call).
    local = (CdrDeliveryLog.query
             .filter_by(service_request_guid=receipt_token)
             .first())
    if local:
        return True, 'local'

    # Also try receipt_token == inbound_observation_guid (the per-row
    # receipt interpretation). The FK is nullable after phase 1's
    # migration but is still set until phase 5 deletion runs.
    local_by_inbound = (CdrDeliveryLog.query
                        .filter_by(inbound_observation_guid=receipt_token)
                        .first())
    if local_by_inbound:
        return True, 'local'

    # 2. Fallback to cdr1 — the inbound row may have been deleted in
    # phase 5; cdr1 still has ingest_raw indexed by source_system_id
    # (== the original inbound_observation_guid).
    base_url = (current_app.config.get('CDR_BASE_URL') or '').rstrip('/')
    service_key = current_app.config.get('GATEWAY_PDHC_SERVICE_KEY', '')
    if not base_url or not service_key:
        return False, 'none'

    try:
        resp = requests.get(
            f'{base_url}/api/v1/ingest/by-source-id/{receipt_token}',
            headers={
                'X-Source-Service': 'gateway.pdhc',
                'X-Service-Key': service_key,
            },
            timeout=current_app.config.get('CDR_TIMEOUT_SECONDS', 30),
        )
    except requests.RequestException as e:
        logger.info('receipt lookup: cdr1 unreachable, treating as miss: %s', e)
        return False, 'none'

    if resp.status_code == 200:
        return True, 'cdr1'
    return False, 'none'
