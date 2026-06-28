"""HTTP client for the analyse-pull proxy: gateway.pdhc → dashboard.pdhc.

Phase 5 of the cdr1 SSOT cutover (ticket #291,
docs/cdr1_ssot_cutover_plan.md §7 → plans/cdr1_analyse_split_plan.md
§5). Replaces ``CdrClient.search_observations`` — gateway no longer
proxies analyse-pull to cdr1; the dashboard's analyse layer
federates over CDR1–6 and returns one Bundle.

Auth scheme is the same as CdrClient: ``X-Source-Service: gateway.pdhc``
+ ``X-Service-Key`` against the receiving service's known-services map.
Dashboard.pdhc adds ``gateway.pdhc → GATEWAY_PDHC_SERVICE_KEY`` so the
same key value gateway already uses can be reused on the dashboard
side — operator copies it across.
"""
import requests
from flask import current_app


class AnalyseUnavailable(Exception):
    """Transient: dashboard unreachable / 5xx / timeout. Caller decides
    whether to surface a hard failure or fall back to a soft empty
    bundle."""


class AnalyseRejected(Exception):
    """Terminal: dashboard 4xx — semantic rejection. Not retryable."""

    def __init__(self, status_code, body):
        self.status_code = status_code
        self.body = body
        super().__init__(
            f"analyse rejected with {status_code}: {body[:200]}")


class AnalyseClient:

    @classmethod
    def _base_url(cls):
        return current_app.config['ANALYSE_BASE_URL'].rstrip('/')

    @classmethod
    def _headers(cls, request_id):
        key = current_app.config.get('GATEWAY_PDHC_SERVICE_KEY', '')
        h = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'X-Source-Service': 'gateway.pdhc',
            'X-Request-Id': request_id,
        }
        if key:
            h['X-Service-Key'] = key
        return h

    @classmethod
    def search_observations(cls, service_request_guids, *, patient=None,
                            request_id=''):
        """GET /api/v1/observations from dashboard.pdhc analyse layer.

        Gateway pre-computes which service-request guids belong to the
        requested organisation (via contract.pdhc lookups) and asks
        analyse only for those; analyse federates over CDR1–6 and
        returns the merged FHIR R5 searchset Bundle.

        Returns the parsed JSON Bundle on success, an empty Bundle on
        empty input. Raises AnalyseRejected on 4xx, AnalyseUnavailable
        on 5xx / network errors.
        """
        if not service_request_guids:
            from datetime import datetime, timezone
            return {
                'resourceType': 'Bundle',
                'type': 'searchset',
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'total': 0,
                'entry': [],
            }

        url = f"{cls._base_url()}/api/v1/observations"
        timeout = current_app.config.get('ANALYSE_TIMEOUT_SECONDS', 30)
        params = [('service_request', g) for g in service_request_guids]
        if patient:
            params.append(('patient', patient))

        try:
            resp = requests.get(
                url, params=params,
                headers=cls._headers(request_id), timeout=timeout,
            )
        except requests.RequestException as e:
            raise AnalyseUnavailable(str(e)) from e

        if 200 <= resp.status_code < 300:
            try:
                return resp.json()
            except ValueError:
                return {}
        if 400 <= resp.status_code < 500:
            raise AnalyseRejected(resp.status_code, resp.text)
        raise AnalyseUnavailable(
            f"HTTP {resp.status_code}: {resp.text[:200]}")
