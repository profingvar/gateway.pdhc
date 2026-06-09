"""Push delivery service.

When the gateway needs to actively push data to a provider (push mode
delivery), this service handles the outbound delivery:

1. Build FHIR Bundle with ServiceRequest, CarePlan, grant_token
2. POST to provider's push_endpoint_url
3. Mutual auth via X-Push-Secret header
4. Update delivery status
5. Audit trail with correlation ID
"""
import uuid
import logging
from datetime import datetime, timezone
from flask import current_app, request as flask_request

import requests as http_requests

from ..models import AuditLog
from ..extensions import db

logger = logging.getLogger(__name__)


class PushDeliveryResult:
    def __init__(self, success, status_code=None, error=None,
                 correlation_id=None, receipt_guid=None):
        self.success = success
        self.status_code = status_code
        self.error = error
        self.correlation_id = correlation_id
        self.receipt_guid = receipt_guid

    def to_dict(self):
        return {
            'success': self.success,
            'status_code': self.status_code,
            'error': self.error,
            'correlation_id': self.correlation_id,
            'receipt_guid': self.receipt_guid,
        }


class PushService:

    @staticmethod
    def push_to_provider(push_endpoint_url, bundle, push_secret,
                         patient_guid=None, provider_org_guid=None):
        """Push a FHIR Bundle to a provider's endpoint.

        Args:
            push_endpoint_url: The provider's registered push endpoint
            bundle: The FHIR Bundle dict to deliver
            push_secret: Mutual auth secret for X-Push-Secret header
            patient_guid: For audit trail (data_subject_guid)
            provider_org_guid: For audit trail (actor_guid)

        Returns:
            PushDeliveryResult
        """
        correlation_id = str(uuid.uuid4())
        receipt_guid = str(uuid.uuid4())
        timeout = current_app.config.get('PUSH_TIMEOUT_SECONDS', 30)
        max_retries = current_app.config.get('PUSH_RETRY_COUNT', 3)

        headers = {
            'Content-Type': 'application/json',
            'X-Push-Secret': push_secret,
            'X-Correlation-Id': correlation_id,
            'X-Receipt-Guid': receipt_guid,
        }

        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                resp = http_requests.post(
                    push_endpoint_url,
                    json=bundle,
                    headers=headers,
                    timeout=timeout,
                )

                if resp.status_code in (200, 201, 202):
                    _audit_push(
                        'bundle.pushed', provider_org_guid, patient_guid,
                        correlation_id, receipt_guid,
                        {'status_code': resp.status_code, 'attempt': attempt},
                    )
                    return PushDeliveryResult(
                        success=True,
                        status_code=resp.status_code,
                        correlation_id=correlation_id,
                        receipt_guid=receipt_guid,
                    )

                last_error = f'HTTP {resp.status_code}: {resp.text[:200]}'
                logger.warning(
                    'Push attempt %d/%d failed: %s',
                    attempt, max_retries, last_error,
                )

            except http_requests.ConnectionError as e:
                last_error = f'Connection refused: {str(e)[:200]}'
                logger.warning(
                    'Push attempt %d/%d failed: %s',
                    attempt, max_retries, last_error,
                )
            except http_requests.Timeout:
                last_error = f'Timeout after {timeout}s'
                logger.warning(
                    'Push attempt %d/%d timed out',
                    attempt, max_retries,
                )
            except Exception as e:
                last_error = str(e)[:200]
                logger.error('Push attempt %d/%d error: %s', attempt, max_retries, e)

        # All retries exhausted
        _audit_push(
            'bundle.push_failed', provider_org_guid, patient_guid,
            correlation_id, receipt_guid,
            {'error': last_error, 'attempts': max_retries},
        )
        return PushDeliveryResult(
            success=False,
            error=last_error,
            correlation_id=correlation_id,
            receipt_guid=receipt_guid,
        )

    @staticmethod
    def send_receipt_to_provider(push_endpoint_url, push_secret, receipt_data):
        """Push a receipt to the provider that owns this PAT.

        This implements tilläggsuppdrag 1 — fire-and-forget receipt delivery.

        Routing is per-PAT, not global: request.pdhc stores a
        `push_endpoint_url` and `push_auth_key` on each PAT record, and
        the gateway validates the PAT through request.pdhc which returns
        those values. That way a single gateway can serve many providers
        (CGM, provider1, …) without config changes.

        Args:
            push_endpoint_url: Provider's push endpoint from the PAT
                record. Gateway derives the receipts URL by swapping the
                last path segment to `receipts/ingest`.
            push_secret: Mutual auth secret from the PAT record. Sent as
                `X-Service-Key` because that's the header the provider's
                receipts endpoint checks. Must match the provider's
                `GATEWAY_SERVICE_KEY` or `PUSH_SECRET` config.
            receipt_data: Receipt dict to deliver.

        Returns:
            bool: True if delivered, False otherwise.
        """
        if not push_endpoint_url:
            logger.warning('send_receipt_to_provider called without push_endpoint_url')
            return False
        if not push_secret:
            logger.warning('send_receipt_to_provider called without push_secret')
            return False

        # Derive the receipts URL. The PAT's push_endpoint_url points at
        # the bundle-push ingress (e.g. .../api/v1/inbound/push); the
        # receipts ingress lives at .../api/v1/receipts/ingest on the
        # same host. We strip '/inbound/push' (or the last two segments)
        # and append '/receipts/ingest'.
        #
        # This is a deliberate heuristic that encodes the current PDHC
        # provider-side convention. When providers start registering a
        # separate receipt URL, request.pdhc can add a
        # `receipt_endpoint_url` column and this derivation disappears.
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(push_endpoint_url)
        path = parsed.path
        if path.endswith('/inbound/push'):
            base = path[: -len('/inbound/push')]
        else:
            # Fallback: drop the last segment.
            base = path.rsplit('/', 1)[0]
        if not base.startswith('/'):
            base = '/' + base
        receipts_path = base.rstrip('/') + '/receipts/ingest'
        receipts_url = urlunparse(parsed._replace(path=receipts_path))

        try:
            resp = http_requests.post(
                receipts_url,
                json=receipt_data,
                headers={
                    'Content-Type': 'application/json',
                    'X-Service-Key': push_secret,
                },
                timeout=10,
            )
            if resp.status_code in (200, 201, 202):
                logger.info('Receipt delivered to %s: %s',
                            receipts_url, receipt_data.get('receipt_guid'))
                return True
            logger.warning('Receipt delivery to %s failed: HTTP %s — %s',
                           receipts_url, resp.status_code, resp.text[:200])
            return False
        except Exception as e:
            logger.warning('Receipt delivery to %s failed: %s', receipts_url, e)
            return False


def _audit_push(event_type, provider_org_guid, patient_guid,
                correlation_id, receipt_guid, details):
    """Log a push delivery event."""
    try:
        audit = AuditLog(
            event_type=event_type,
            actor_guid=provider_org_guid,
            data_subject_guid=patient_guid,
            resource_guid=receipt_guid,
            correlation_id=correlation_id,
            payload_snapshot=details,
        )
        db.session.add(audit)
        db.session.commit()
    except Exception:
        db.session.rollback()
