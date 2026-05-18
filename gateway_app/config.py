import os
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()


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
