"""Composite key / DataExchangeGrant validation service.

Delegates grant validation to request.pdhc via its internal API.
The HMAC_SECRET never leaves request.pdhc — gateway only sends the
grant_token and receives a valid/invalid verdict.
"""
import logging
import requests as http_requests
from flask import current_app

logger = logging.getLogger(__name__)


class GrantValidationResult:
    """Result of a composite key validation."""

    def __init__(self, valid, error=None, error_code=None,
                 contract_guid=None, grant_type=None, uses_remaining=None):
        self.valid = valid
        self.error = error
        self.error_code = error_code or ('GRANT_INVALID' if not valid else None)
        self.contract_guid = contract_guid
        self.grant_type = grant_type
        self.uses_remaining = uses_remaining


class GrantValidationService:
    """Validates grant tokens by delegating to request.pdhc internal API."""

    @staticmethod
    def validate(service_request_guid, patient_guid, organisation_guid,
                 grant_token, expires_at_iso=None):
        """Validate the composite key via request.pdhc.

        Args:
            service_request_guid: The ServiceRequest GUID
            patient_guid: The patient GUID
            organisation_guid: The provider organisation GUID
            grant_token: The grant token to verify
            expires_at_iso: Optional (ignored — request.pdhc checks expiry)

        Returns:
            GrantValidationResult
        """
        # Check required fields
        missing = []
        if not service_request_guid:
            missing.append('service_request_guid')
        if not patient_guid:
            missing.append('patient_guid')
        if not organisation_guid:
            missing.append('organisation_guid')
        if not grant_token:
            missing.append('grant_token')

        if missing:
            return GrantValidationResult(
                valid=False,
                error=f'Missing composite key fields: {", ".join(missing)}',
                error_code='COMPOSITE_KEY_INCOMPLETE',
            )

        # Call request.pdhc internal API
        base_url = current_app.config.get('REQUEST_SERVICE_URL', '')
        service_key = current_app.config.get('REQUEST_INTERNAL_SERVICE_KEY', '')

        if not base_url or not service_key:
            logger.error('REQUEST_SERVICE_URL or REQUEST_INTERNAL_SERVICE_KEY not configured')
            return GrantValidationResult(
                valid=False,
                error='Server configuration error',
                error_code='SERVER_ERROR',
            )

        try:
            resp = http_requests.post(
                f'{base_url}/internal/grant/validate',
                headers={
                    'X-Service-Key': service_key,
                    'Content-Type': 'application/json',
                },
                json={
                    'sr_guid': service_request_guid,
                    'patient_guid': patient_guid,
                    'org_guid': organisation_guid,
                    'grant_token': grant_token,
                },
                timeout=10,
            )
        except http_requests.RequestException as e:
            logger.error('Grant validation call failed: %s', e)
            return GrantValidationResult(
                valid=False,
                error='Grant validation service unavailable',
                error_code='SERVICE_UNAVAILABLE',
            )

        if resp.status_code == 401:
            logger.error('Grant validation auth rejected — check REQUEST_INTERNAL_SERVICE_KEY')
            return GrantValidationResult(
                valid=False,
                error='Server configuration error',
                error_code='SERVER_ERROR',
            )

        data = resp.json()

        if resp.status_code == 400:
            return GrantValidationResult(
                valid=False,
                error=data.get('error', 'Validation failed'),
                error_code='COMPOSITE_KEY_INCOMPLETE',
            )

        if not data.get('valid'):
            return GrantValidationResult(
                valid=False,
                error=data.get('error', 'Invalid grant token'),
                error_code='GRANT_TOKEN_INVALID',
            )

        return GrantValidationResult(
            valid=True,
            contract_guid=data.get('contract_guid'),
            grant_type=data.get('grant_type'),
            uses_remaining=data.get('uses_remaining'),
        )
