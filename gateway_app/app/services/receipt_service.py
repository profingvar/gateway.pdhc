"""Receipt acknowledgement service.

Handles provider acknowledgement of delivery receipts.
"""
import logging
from flask import g, request as flask_request
from ..models import InboundObservation, AuditLog
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
        record = InboundObservation.query.filter_by(
            service_request_guid=receipt_token,
            provider_org_guid=provider_org_guid,
        ).first()

        # Audit the ack regardless of whether we have a local record
        audit = AuditLog(
            event_type='receipt.acknowledged',
            actor_guid=provider_org_guid,
            resource_guid=receipt_token,
            ip_address=flask_request.remote_addr,
            correlation_id=flask_request.headers.get('X-Correlation-Id'),
            payload_snapshot={
                'receipt_token': receipt_token,
                'has_local_record': record is not None,
            },
        )
        db.session.add(audit)
        db.session.commit()

        return {
            'status': 'acknowledged',
            'receipt_token': receipt_token,
        }
