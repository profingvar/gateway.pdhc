import os
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()


def _session_cookie_secure(production: bool) -> bool:
    """#816 — mark the session cookie Secure so it never travels in clear.

    This cookie carries the SSO token and the access blob. nginx terminates
    TLS and proxies plain HTTP to gunicorn, so Flask sees an http request and
    cannot infer the flag; it has to be set deliberately.

    The vhost's `return 301 https://` does not make it unnecessary: a browser
    holding a non-Secure cookie attaches it to the cleartext request and only
    THEN receives the redirect, so the token is already on the wire. HSTS is
    what prevents that first request, and it is absent on 9 of 11 vhosts
    (operator-only, #816 Part B).

    Off outside production, where the service is plain http://localhost and a
    Secure cookie would simply never be stored.

    Overridable, because the flag has one real cost: production reached
    directly on 127.0.0.1:<port> through an ssh forward is plain HTTP, so a
    logged-in page cannot be debugged that way while this is on. An EMPTY
    value counts as unset rather than false — reading `FOO=` as false would
    silently disable it in production.
    """
    raw = os.environ.get('SESSION_COOKIE_SECURE', '').strip()
    if raw:
        return raw.lower() in ('1', 'true', 'yes')
    return production


class Config:
    SECRET_KEY = os.environ.get('FLASK_SECRET_KEY', 'change-me')
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL')
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Ticket #93: cap the local cookie lifetime so a stale session cookie
    # can't outlive its SSO-side counterpart by weeks. SSO-side idle timeout
    # (10 min) is the primary gate via sso_service.get_access_blob(); this is
    # belt-and-braces.
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)

    # Upstream (request.pdhc) for PAT validation, GUID resolution, grant validation
    REQUEST_SERVICE_URL = os.environ.get('REQUEST_SERVICE_URL', 'https://request.pdhc.se/api/v1')
    REQUEST_INTERNAL_SERVICE_KEY = os.environ.get('REQUEST_INTERNAL_SERVICE_KEY', '')
    GUID_CACHE_TTL_SECONDS = int(os.environ.get('GUID_CACHE_TTL_SECONDS', '3600'))
    GRANT_CACHE_TTL_SECONDS = int(os.environ.get('GRANT_CACHE_TTL_SECONDS', '60'))

    # Upstream (contract.pdhc) for contract scope validation
    CONTRACT_SERVICE_URL = os.environ.get('CONTRACT_SERVICE_URL', 'https://contract.pdhc.se')
    CONTRACT_INTERNAL_SERVICE_KEY = os.environ.get('CONTRACT_INTERNAL_SERVICE_KEY', '')

    # Bootstrap
    BOOTSTRAP_SU_API_KEY = os.environ.get('BOOTSTRAP_SU_API_KEY')

    # Push settings
    PUSH_TIMEOUT_SECONDS = int(os.environ.get('PUSH_TIMEOUT_SECONDS', '30'))
    PUSH_RETRY_COUNT = int(os.environ.get('PUSH_RETRY_COUNT', '3'))

    # Downstream (provider.pdhc) for receipt delivery
    PROVIDER_SERVICE_URL = os.environ.get('PROVIDER_SERVICE_URL', 'http://localhost:9070/api/v1')

    # Vector storage (experimental)
    PGVECTOR_DIMENSIONS = int(os.environ.get('PGVECTOR_DIMENSIONS', '384'))
    EMBEDDING_MODEL = os.environ.get('EMBEDDING_MODEL', 'local')

    # SSO
    SSO_BASE_URL = os.environ.get('SSO_BASE_URL', 'https://sso.pdhc.se')
    SSO_CLIENT_ID = os.environ.get('SSO_CLIENT_ID', '')
    SSO_CLIENT_SECRET = os.environ.get('SSO_CLIENT_SECRET', '')
    SSO_CALLBACK_URL = os.environ.get('SSO_CALLBACK_URL', 'https://gateway.pdhc.se/auth/callback')
    AUTH_DISABLED = os.environ.get('AUTH_DISABLED', '').lower() in ('1', 'true', 'yes')

    # ---- #816: session cookie hardening ------------------------------
    SESSION_COOKIE_SECURE = _session_cookie_secure(not AUTH_DISABLED)
    # No script should read this cookie. Flask already defaults it True;
    # stated so a future edit argues with a line, not an absence.
    SESSION_COOKIE_HTTPONLY = True
    # 'Lax', NOT 'Strict', and load-bearing: SSO login finishes with a
    # top-level GET redirect from sso.pdhc.se back to SSO_CALLBACK_URL
    # above. Lax sends the cookie on a top-level navigation; Strict
    # withholds it and breaks login with no error anyone can read.
    SESSION_COOKIE_SAMESITE = 'Lax'

    # Downstream cdr.pdhc (cdr1) — single source of truth that simulates
    # Cambio CDR. Insert-then-send forwarder pushes new inbound
    # observations onward; APScheduler polls cdr_delivery_log every
    # CDR_FORWARDING_INTERVAL_SECONDS.
    CDR_BASE_URL = os.environ.get('CDR_BASE_URL', 'http://cdr_pdhc_app:9046')
    CDR_TIMEOUT_SECONDS = int(os.environ.get('CDR_TIMEOUT_SECONDS', '30'))
    CDR_FORWARDING_ENABLED = os.environ.get(
        'CDR_FORWARDING_ENABLED', '').lower() in ('1', 'true', 'yes')
    CDR_FORWARDING_INTERVAL_SECONDS = int(
        os.environ.get('CDR_FORWARDING_INTERVAL_SECONDS', '60'))
    # SSOT phase 5 (#284) — when true, the forwarder deletes the
    # InboundObservation row immediately after marking the
    # CdrDeliveryLog row as delivered. cdr1 then becomes the sole
    # source of truth for observation data. Default false so first
    # deploy is dark; flip after running flask delete-already-delivered
    # for the historical backlog.
    CDR_FORWARDING_DELETE_AFTER_DELIVERY = os.environ.get(
        'CDR_FORWARDING_DELETE_AFTER_DELIVERY', '').lower() in ('1', 'true', 'yes')
    # cdr1 expects X-Source-Service: gateway.pdhc + X-Service-Key. This
    # key MUST match cdr.pdhc/cdr_app/app/api/auth.py's
    # GATEWAY_PDHC_SERVICE_KEY config — operator copies it across.
    # Since #291 the same key value also authenticates the analyse-pull
    # call (gateway → dashboard.pdhc), so the operator must propagate
    # it to dashboard.pdhc's .env too.
    GATEWAY_PDHC_SERVICE_KEY = os.environ.get('GATEWAY_PDHC_SERVICE_KEY', '')

    # Analyse layer (dashboard.pdhc) — gateway's /api/v1/observations
    # search now proxies here (was cdr1 in #282). Federated cohort
    # reads across CDR1–6 are analyse's job; cdr1 only owns per-point
    # storage.
    ANALYSE_BASE_URL = os.environ.get(
        'ANALYSE_BASE_URL', 'http://host.docker.internal:9110')
    ANALYSE_TIMEOUT_SECONDS = int(
        os.environ.get('ANALYSE_TIMEOUT_SECONDS', '30'))


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    BOOTSTRAP_SU_API_KEY = 'test-su-key'
    REQUEST_SERVICE_URL = 'http://mock-request-service/api/v1'
    REQUEST_INTERNAL_SERVICE_KEY = 'test-request-key'
    CONTRACT_SERVICE_URL = 'http://mock-contract-service'
    CONTRACT_INTERNAL_SERVICE_KEY = 'test-contract-key'
    PGVECTOR_DIMENSIONS = 384
    AUTH_DISABLED = True
    # #816: must be restated, not inherited. SESSION_COOKIE_SECURE is computed
    # in Config's body from the ENVIRONMENT's AUTH_DISABLED, and a class
    # attribute does not recompute when a subclass overrides one of its
    # inputs. So overriding AUTH_DISABLED here would otherwise leave the
    # cookie Secure under test — and the test client speaks plain http, so
    # the cookie would never be stored and every session-dependent test would
    # fail for a reason unrelated to what it tests.
    SESSION_COOKIE_SECURE = False
