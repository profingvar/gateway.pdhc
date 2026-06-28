"""Service request completion tracking.

Tracks whether a ServiceRequest has been fully delivered (all expected
transactions received) or has expired due to time. Updates the
service_request_status table after each observation ingestion.

Completion logic:
- If expected_transactions is known (from GUID resolution) and
  delivered_transactions >= expected_transactions → status = 'completed'
- If grant has expired and delivered_transactions > 0 → status = 'partial'
- If grant has expired and delivered_transactions == 0 → status = 'expired'
- Otherwise → status = 'active'
"""
import logging
from datetime import datetime, timezone
from ..models import ServiceRequestStatus, CdrDeliveryLog
from ..extensions import db

logger = logging.getLogger(__name__)


class RequestCompletionService:

    @staticmethod
    def track_delivery(service_request_guid, patient_guid,
                       provider_org_guid, contract_guid,
                       observations_count, expires_at_iso=None,
                       transaction_guids=None):
        """Update delivery tracking after observations are stored.

        Args:
            service_request_guid: the SR being tracked
            patient_guid: patient GUID
            provider_org_guid: provider org GUID
            contract_guid: contract GUID
            observations_count: number of observations just stored
            expires_at_iso: optional grant expiry (ISO-8601)
            transaction_guids: list of transaction GUIDs in this delivery
        """
        srs = ServiceRequestStatus.query.filter_by(
            service_request_guid=service_request_guid,
        ).first()

        now = datetime.now(timezone.utc)

        if srs is None:
            srs = ServiceRequestStatus(
                service_request_guid=service_request_guid,
                patient_guid=patient_guid,
                provider_org_guid=provider_org_guid,
                contract_guid=contract_guid,
                status='active',
                delivered_transactions=0,
                total_observations=0,
                first_received_at=now,
            )
            db.session.add(srs)
            db.session.flush()

        # Update counters
        srs.total_observations = (srs.total_observations or 0) + observations_count
        srs.last_received_at = now

        # Count distinct transaction GUIDs delivered for this SR
        if transaction_guids:
            # #298: distinct-transaction count moves to CdrDeliveryLog
            # (the only ingest queue since #296).
            existing_txns = db.session.query(
                db.func.count(db.distinct(CdrDeliveryLog.transaction_guid))
            ).filter(
                CdrDeliveryLog.service_request_guid == service_request_guid,
                CdrDeliveryLog.transaction_guid.isnot(None),
            ).scalar() or 0
            srs.delivered_transactions = existing_txns

        # Update grant expiry if provided
        if expires_at_iso and not srs.grant_expires_at:
            try:
                expires_at = datetime.fromisoformat(expires_at_iso)
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                srs.grant_expires_at = expires_at
            except (ValueError, TypeError):
                pass

        # Evaluate status
        RequestCompletionService._evaluate_status(srs)

        db.session.commit()
        return srs

    @staticmethod
    def set_expected_transactions(service_request_guid, count):
        """Set the expected number of transactions for a SR.

        Called when GUID resolution reveals the PlanDefinition activity count.
        """
        srs = ServiceRequestStatus.query.filter_by(
            service_request_guid=service_request_guid,
        ).first()
        if srs and count:
            srs.expected_transactions = count
            RequestCompletionService._evaluate_status(srs)
            db.session.commit()
        return srs

    @staticmethod
    def check_expirations():
        """Check all active requests for expiration. Returns count of newly expired."""
        now = datetime.now(timezone.utc)
        active = ServiceRequestStatus.query.filter(
            ServiceRequestStatus.status == 'active',
            ServiceRequestStatus.grant_expires_at.isnot(None),
            ServiceRequestStatus.grant_expires_at < now,
        ).all()

        count = 0
        for srs in active:
            if srs.delivered_transactions > 0:
                srs.status = 'partial'
            else:
                srs.status = 'expired'
            srs.expired_at = now
            count += 1

        if count:
            db.session.commit()
            logger.info('Marked %d service requests as expired/partial', count)
        return count

    @staticmethod
    def get_all(status_filter=None, page=1, per_page=50):
        """Get paginated list of tracked service requests."""
        query = ServiceRequestStatus.query.order_by(
            ServiceRequestStatus.last_received_at.desc(),
        )
        if status_filter:
            query = query.filter(ServiceRequestStatus.status == status_filter)
        return query.paginate(page=page, per_page=per_page, error_out=False)


    @staticmethod
    def _notify_request_completed(sr_guid):
        """Fire-and-forget callback to request.pdhc to flip the SR to completed."""
        try:
            import requests as http_requests
            from flask import current_app
            base = current_app.config.get('REQUEST_SERVICE_URL', '').rstrip('/')
            key = current_app.config.get('REQUEST_INTERNAL_SERVICE_KEY', '')
            if not base or not key:
                logger.warning('REQUEST_SERVICE_URL or REQUEST_INTERNAL_SERVICE_KEY not set; cannot notify completion for %s', sr_guid)
                return
            url = f"{base}/internal/service-request/{sr_guid}/complete"
            resp = http_requests.post(url, headers={'X-Service-Key': key}, timeout=5)
            if resp.status_code not in (200, 404):
                logger.warning('Notify completion %s -> %d %s', sr_guid, resp.status_code, resp.text[:200])
        except Exception as e:
            logger.warning('Notify completion failed for %s: %s', sr_guid, e)

    @staticmethod
    def _evaluate_status(srs):
        """Evaluate and update the status of a ServiceRequestStatus."""
        now = datetime.now(timezone.utc)

        # Already completed — don't change
        if srs.status == 'completed':
            return

        # Check completion: all expected transactions delivered
        if (srs.expected_transactions
                and srs.delivered_transactions >= srs.expected_transactions):
            srs.status = 'completed'
            srs.completed_at = now
            logger.info('ServiceRequest %s marked as completed (%d/%d)',
                        srs.service_request_guid,
                        srs.delivered_transactions,
                        srs.expected_transactions)
            RequestCompletionService._notify_request_completed(srs.service_request_guid)
            return

        # Check expiry
        if srs.grant_expires_at and now > srs.grant_expires_at:
            if srs.delivered_transactions > 0:
                srs.status = 'partial'
            else:
                srs.status = 'expired'
            srs.expired_at = now
            return

        # Still active
        srs.status = 'active'
