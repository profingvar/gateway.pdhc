"""HTTP client for forwarding observations gateway.pdhc → cdr.pdhc.

Mirrors the auth scheme of cdr.pdhc's existing ingest endpoint
(X-Source-Service + X-Service-Key) — same as sim.pdhc/sim/client.py and
the registered source-service auth in
cdr.pdhc/cdr_app/app/api/auth.py.

Retry/backoff is intentionally NOT done here — the cdr_forwarder
worker owns retry semantics (per-row exponential backoff via
attempt_count). This client only does single HTTP calls and reports
errors back as exceptions or non-2xx status codes.
"""
import logging
import requests
from flask import current_app

logger = logging.getLogger(__name__)


class CdrUnavailable(Exception):
    """Transient: cdr1 unreachable / 5xx / timeout. Retryable."""


class CdrRejected(Exception):
    """Terminal: cdr1 4xx — semantic rejection. Not retryable."""

    def __init__(self, status_code, body):
        self.status_code = status_code
        self.body = body
        super().__init__(f"cdr1 rejected with {status_code}: {body[:200]}")


class CdrClient:

    @classmethod
    def _base_url(cls):
        return current_app.config['CDR_BASE_URL'].rstrip('/')

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
    def deliver_one(cls, payload, request_id):
        """POST a single observation to cdr1.

        Returns the parsed JSON response body on success (cdr1 echoes
        back ingest metadata). Raises CdrRejected on 4xx,
        CdrUnavailable on 5xx / network errors.
        """
        url = f"{cls._base_url()}/api/v1/ingest"
        timeout = current_app.config.get('CDR_TIMEOUT_SECONDS', 30)

        try:
            resp = requests.post(
                url, json=payload, headers=cls._headers(request_id),
                timeout=timeout,
            )
        except requests.RequestException as e:
            raise CdrUnavailable(str(e)) from e

        if 200 <= resp.status_code < 300:
            try:
                return resp.json()
            except ValueError:
                return {}
        if 400 <= resp.status_code < 500:
            raise CdrRejected(resp.status_code, resp.text)
        # 5xx / unexpected
        raise CdrUnavailable(f"HTTP {resp.status_code}: {resp.text[:200]}")

    @classmethod
    def deliver_batch(cls, items, request_id):
        """POST up to 100 observations in one call.

        items: list of dicts matching cdr1's ingest payload shape.
        Returns the parsed JSON response on success.
        """
        if not items:
            return {}
        if len(items) > 100:
            raise ValueError(
                f"cdr1 batch limit is 100, got {len(items)}; "
                "let the caller chunk before invoking deliver_batch.")
        url = f"{cls._base_url()}/api/v1/ingest/batch"
        timeout = current_app.config.get('CDR_TIMEOUT_SECONDS', 30)

        try:
            resp = requests.post(
                url, json={'items': items},
                headers=cls._headers(request_id), timeout=timeout,
            )
        except requests.RequestException as e:
            raise CdrUnavailable(str(e)) from e

        if 200 <= resp.status_code < 300:
            try:
                return resp.json()
            except ValueError:
                return {}
        if 400 <= resp.status_code < 500:
            raise CdrRejected(resp.status_code, resp.text)
        raise CdrUnavailable(f"HTTP {resp.status_code}: {resp.text[:200]}")
