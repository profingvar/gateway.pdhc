"""FHIR R5 Observation validation.

Validates the observations array from a provider report submission.
Required fields: transaction_guid, concept_guid, value, response_type.
Optional fields (passed through, no validation): unit, notes, recorded_at.
Validates response_type constraints.

Aligned with provider.pdhc's guided_response.py submission format.
"""
import logging

logger = logging.getLogger(__name__)

# #871: this was a SIXTH hand-written copy of the response_type vocabulary.
# It happened to agree with the builder's (same six values, different order),
# but agreeing by luck is what the ticket is about — the PRODUCER
# (`_extract_qr_answer`) disagreed with both, and nothing noticed.
#
# The aliases in `response_types` are deliberately NOT applied here. They are
# an internal bridge for rows gateway itself already queued; the submission
# contract providers code against (provider.pdhc's guided_response.py) stays
# canonical, so a provider sending `choice` is still told what the six values
# are rather than quietly accepted into a widened wire format.
from .response_types import CANONICAL as VALID_RESPONSE_TYPES


class ObservationValidationResult:
    def __init__(self):
        self.valid = True
        self.errors = []

    def add_error(self, index, field, message):
        self.valid = False
        self.errors.append({
            'observation_index': index,
            'field': field,
            'message': message,
        })


class ObservationValidator:
    """Validates observations from a provider report payload."""

    @staticmethod
    def validate_observations(observations, observation_guid=None):
        """Validate an array of observations.

        Args:
            observations: list of observation dicts from report_payload
            observation_guid: optional parent observation GUID for logging

        Returns:
            ObservationValidationResult
        """
        result = ObservationValidationResult()

        if not isinstance(observations, list):
            result.add_error(-1, 'observations', 'Must be an array')
            return result

        if len(observations) == 0:
            result.add_error(-1, 'observations', 'At least one observation required')
            return result

        for i, obs in enumerate(observations):
            if not isinstance(obs, dict):
                result.add_error(i, 'observation', 'Must be an object')
                continue

            ObservationValidator._validate_single(obs, i, result)

        # Log validation results
        if observation_guid:
            ObservationValidator._log_result(observation_guid, result)

        return result

    @staticmethod
    def _validate_single(obs, index, result):
        """Validate a single observation entry."""
        # Required fields
        if not obs.get('transaction_guid'):
            result.add_error(index, 'transaction_guid', 'Required field missing')

        if not obs.get('concept_guid'):
            result.add_error(index, 'concept_guid', 'Required field missing')

        if 'value' not in obs:
            result.add_error(index, 'value', 'Required field missing')

        response_type = obs.get('response_type')
        if not response_type:
            result.add_error(index, 'response_type', 'Required field missing')
        elif response_type not in VALID_RESPONSE_TYPES:
            result.add_error(
                index, 'response_type',
                f'Invalid response_type "{response_type}". '
                f'Must be one of: {", ".join(VALID_RESPONSE_TYPES)}',
            )

        # Type-specific validation
        if response_type and 'value' in obs:
            ObservationValidator._validate_value_type(
                obs['value'], response_type, index, result,
            )

    @staticmethod
    def _validate_value_type(value, response_type, index, result):
        """Validate that the value matches the declared response_type."""
        if value is None:
            return  # null values allowed (missing data)

        if response_type == 'numeric':
            if not isinstance(value, (int, float)):
                result.add_error(
                    index, 'value',
                    f'Expected numeric value, got {type(value).__name__}',
                )

        elif response_type == 'boolean':
            if not isinstance(value, bool):
                result.add_error(
                    index, 'value',
                    f'Expected boolean value, got {type(value).__name__}',
                )

        elif response_type == 'text':
            if not isinstance(value, str):
                result.add_error(
                    index, 'value',
                    f'Expected text value, got {type(value).__name__}',
                )

        elif response_type == 'categorical':
            if not isinstance(value, str):
                result.add_error(
                    index, 'value',
                    f'Expected categorical string value, got {type(value).__name__}',
                )

        elif response_type == 'dateTime':
            if not isinstance(value, str):
                result.add_error(
                    index, 'value',
                    f'Expected dateTime string, got {type(value).__name__}',
                )

        elif response_type == 'graph':
            if not isinstance(value, str):
                result.add_error(
                    index, 'value',
                    f'Expected graph marker string, got {type(value).__name__}',
                )

        else:
            # #871: unreachable TODAY — `_validate_single` rejects anything
            # outside VALID_RESPONSE_TYPES before calling this. The branch
            # exists because the chain above is a hand-written mirror of that
            # tuple, and a value SILENTLY validated by nothing is the failure
            # this ticket is about. If a seventh type is added to CANONICAL and
            # not here, this says so instead of passing.
            result.add_error(
                index, 'response_type',
                f'No value validation implemented for response_type '
                f'"{response_type}"',
            )

