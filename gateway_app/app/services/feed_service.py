"""Provider feed service.

Proxies the feed and download endpoints from request.pdhc so that
providers only need to talk to the gateway.  The provider's PAT
(already validated) is forwarded to request.pdhc.

Feed returns metadata only (GDPR data minimization) — no patient
names, diagnoses, or clinical data.
"""
import logging
from flask import current_app, g, request as flask_request

import requests as http_requests

from ..models import AuditLog
from ..extensions import db
from .sso_service import outbound_session_headers

logger = logging.getLogger(__name__)


class FeedService:

    @staticmethod
    def get_feed(raw_token):
        """Proxy the provider feed from request.pdhc.

        Args:
            raw_token: The provider's PAT (forwarded to upstream)

        Returns:
            tuple: (response_dict, status_code)
        """
        base_url = current_app.config['REQUEST_SERVICE_URL']
        url = f'{base_url}/provider/feed'

        # Forward query params (since, limit, cursor)
        params = {}
        if flask_request.args.get('since'):
            params['since'] = flask_request.args['since']
        if flask_request.args.get('limit'):
            params['limit'] = flask_request.args['limit']
        if flask_request.args.get('cursor'):
            params['cursor'] = flask_request.args['cursor']
        # #1043: narrow to one patient's orders (the patient app polls per
        # logged-in patient). request.pdhc applies it inside the PAT's own
        # matches, so it can only ever return fewer items.
        if flask_request.args.get('patient_guid'):
            params['patient_guid'] = flask_request.args['patient_guid']

        try:
            resp = http_requests.get(
                url,
                headers={
                    'X-Provider-Token': raw_token,
                    'X-Correlation-Id': flask_request.headers.get('X-Correlation-Id', ''),
                    **outbound_session_headers(),  # X2 #408
                },
                params=params,
                timeout=current_app.config.get('PUSH_TIMEOUT_SECONDS', 30),
            )

            # Audit the feed access
            _audit_feed('feed.accessed', resp.status_code)

            if resp.status_code == 200:
                return resp.json(), 200
            return {'code': 'UPSTREAM_ERROR', 'message': resp.text}, resp.status_code

        except http_requests.ConnectionError:
            logger.warning('request.pdhc unreachable for feed at %s', url)
            return {'code': 'UPSTREAM_UNREACHABLE', 'message': 'request.pdhc is unreachable'}, 502
        except Exception as e:
            logger.error('Feed proxy error: %s', e)
            return {'code': 'INTERNAL_ERROR', 'message': str(e)}, 500

    @staticmethod
    def download_bundle(service_request_guid, raw_token):
        """Proxy a bundle download from request.pdhc.

        Args:
            service_request_guid: The SR to download
            raw_token: The provider's PAT

        Returns:
            tuple: (response_dict, status_code)
        """
        base_url = current_app.config['REQUEST_SERVICE_URL']
        url = f'{base_url}/provider/download/{service_request_guid}'

        try:
            resp = http_requests.get(
                url,
                headers={
                    'X-Provider-Token': raw_token,
                    'X-Correlation-Id': flask_request.headers.get('X-Correlation-Id', ''),
                    **outbound_session_headers(),  # X2 #408
                },
                timeout=current_app.config.get('PUSH_TIMEOUT_SECONDS', 30),
            )

            # Audit the download
            _audit_feed('bundle.downloaded', resp.status_code,
                        sr_guid=service_request_guid)

            if resp.status_code == 200:
                return resp.json(), 200
            return {'code': 'UPSTREAM_ERROR', 'message': resp.text}, resp.status_code

        except http_requests.ConnectionError:
            logger.warning('request.pdhc unreachable for download at %s', url)
            return {'code': 'UPSTREAM_UNREACHABLE', 'message': 'request.pdhc is unreachable'}, 502
        except Exception as e:
            logger.error('Download proxy error: %s', e)
            return {'code': 'INTERNAL_ERROR', 'message': str(e)}, 500


    @staticmethod
    def post_status(service_request_guid, raw_token, body):
        """Proxy a lifecycle status update to request.pdhc.

        request.pdhc owns ServiceRequest lifecycle (`/provider/status`), but a
        provider only knows gateway.pdhc. Without this route the only path was
        `/provider/report`, which requires observation data — so a status
        with no data (e.g. 'acknowledged') was refused 400 and the requester
        never learned the provider had taken the task. Found on the
        cgm_provider onboarding, 2026-11-18. The body is forwarded unchanged;
        request.pdhc validates the composite key and the PAT's org.
        """
        base_url = current_app.config['REQUEST_SERVICE_URL']
        url = f'{base_url}/provider/status/{service_request_guid}'
        try:
            resp = http_requests.post(
                url,
                json=body,
                headers={
                    'X-Provider-Token': raw_token,
                    'X-Correlation-Id': flask_request.headers.get('X-Correlation-Id', ''),
                    **outbound_session_headers(),
                },
                timeout=current_app.config.get('PUSH_TIMEOUT_SECONDS', 30),
            )
            _audit_feed('provider.status.forwarded', resp.status_code,
                        sr_guid=service_request_guid)
            try:
                return resp.json(), resp.status_code
            except ValueError:
                return ({'code': 'UPSTREAM_ERROR', 'message': resp.text[:300]},
                        resp.status_code if resp.status_code >= 400 else 502)
        except http_requests.ConnectionError:
            logger.warning('request.pdhc unreachable for status at %s', url)
            return {'code': 'UPSTREAM_UNREACHABLE', 'message': 'request.pdhc is unreachable'}, 502
        except Exception as e:
            logger.error('Status proxy error: %s', e)
            return {'code': 'INTERNAL_ERROR', 'message': str(e)}, 500


def _audit_feed(event_type, status_code, sr_guid=None):
    """Log a feed/download event."""
    try:
        audit = AuditLog(
            event_type=event_type,
            actor_guid=g.provider_org_guid if hasattr(g, 'provider_org_guid') else None,
            resource_guid=sr_guid,
            ip_address=flask_request.remote_addr,
            correlation_id=flask_request.headers.get('X-Correlation-Id'),
            payload_snapshot={'status_code': status_code},
        )
        db.session.add(audit)
        db.session.commit()
    except Exception:
        db.session.rollback()
