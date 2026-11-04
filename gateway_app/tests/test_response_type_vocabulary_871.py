"""#871 — one response_type vocabulary, and the corruption it was hiding.

THE TICKET FRAMED THIS AS plan-vs-gateway. It is not. Measured 2026-11-04,
the disagreement was between gateway's own producer and gateway's own
consumers, in this repository, with no network between them:

    producer  report_ingestion._extract_qr_answer   numeric text boolean date datetime choice
    consumer  fhir_observation_builder._SURVEY_TYPES    categorical boolean text dateTime graph
    consumer  observation_validator.VALID_RESPONSE_TYPES  numeric categorical text boolean dateTime graph

``text`` and ``boolean`` matched. Three did not: ``choice`` against
``categorical``, ``datetime`` against ``dateTime`` (a CASE difference), and
``date``, which appeared in no consumer at all.

WHAT THAT COST. `test_categorical_is_codeableconcept_not_quantity` in
`test_fhir_observation_builder.py` calls the quantity-coercion of a coded
value "the core corruption bug" and fixes it under #489 — for the spelling
``categorical``. The questionnaire producer spelled it ``choice``, so every
coded questionnaire answer went to the builder's ``else`` branch, which
blind-``float()``s. A Likert "5" or a coded "100" was therefore written to the
clinical store as a ``valueQuantity``: a NUMBER where a code belonged. The fix
for the corruption existed, was tested, and the producer routed around it.

WHY NO TEST CAUGHT IT. All 31 pre-#871 references to ``response_type`` in this
suite use canonical spellings. Not one uses ``choice``, ``datetime`` or
``date``. The suite exercised the vocabulary the consumers understand and never
the one the producer emits, so the tests and the producer disagreed and the
tests won. ``TestTheProducerConsumerContract`` below is the test that closes
that: it reads the producer's returns from the PARSE TREE and requires every
one of them to be a member of the vocabulary.

SCOPE, measured so the claims here are not wider than the evidence:

  - The defect is live on the QuestionnaireResponse path ONLY.
    `_store_questionnaire_response` never calls the validator, so its children
    go straight to the forwarder and the builder.
  - The provider-report path (`ingest`) validated against the canonical six
    all along and refused anything else at submit time, so ``choice`` could
    never arrive that way.
  - cdr1 has ZERO non-test readers of the field. rosetta's only references are
    test fixtures already using canonical spellings — so normalising
    `build_canonical_observation` sends rosetta what its own test assumes.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import app.services.cdr_forwarder as fwd
from app.services import observation_validator as validator
from app.services.fhir_observation_builder import (build_canonical_observation,
                                                   build_fhir_observation)
from app.services.response_types import (ALIASES, CANONICAL, MEASUREMENT,
                                         SURVEY_TYPES, UnknownResponseType,
                                         is_survey, normalise)

#: The spellings the producer emitted before #871. Named so the controls below
#: are explicit about what they are controlling for.
PRE_871_SPELLINGS = ('choice', 'datetime', 'date')


def _row(fhir_json, **over):
    d = dict(guid="r1", concept_guid="c1", patient_guid="p1",
             service_request_guid=None, contract_guid=None,
             provider_org_guid=None, transaction_guid=None,
             received_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
             fhir_observation_json=fhir_json)
    d.update(over)
    return SimpleNamespace(**d)


class TestTheVocabularyHasOneOwner:
    def test_survey_types_is_DERIVED_not_written(self):
        """The ticket's actual ask. A second hand-maintained tuple is what
        drifted, so the survey set is computed from the canonical one."""
        assert SURVEY_TYPES == tuple(t for t in CANONICAL if t != MEASUREMENT)

    def test_the_derivation_reproduces_the_old_tuple_exactly(self):
        """CONTROL on the refactor itself: the derived set must equal the
        hand-written `_SURVEY_TYPES` it replaced, or this ticket changed
        behaviour for the five values that already worked."""
        assert set(SURVEY_TYPES) == {'categorical', 'boolean', 'text',
                                     'dateTime', 'graph'}

    def test_the_measurement_type_is_not_a_survey_answer(self):
        assert MEASUREMENT == 'numeric'
        assert MEASUREMENT not in SURVEY_TYPES
        assert not is_survey(MEASUREMENT)

    def test_the_validator_reads_the_SAME_object_not_a_copy(self):
        """It held a sixth hand-written copy. Identity, not equality: an equal
        copy is exactly what drifts."""
        assert validator.VALID_RESPONSE_TYPES is CANONICAL


class TestTheProducerConsumerContract:
    """The test that would have caught #871, and the one to keep.

    It reads the producer from the parse tree rather than importing and
    calling it, so it sees every return in the function including ones no
    fixture happens to reach. Comments cannot satisfy it (CLAUDE.md §7): a
    docstring explaining `choice` is not a `Constant` in a `Return`.
    """

    @staticmethod
    def _producer_response_types():
        import ast
        import inspect
        from app.services import report_ingestion
        src = inspect.getsource(report_ingestion._extract_qr_answer)
        tree = ast.parse(src.lstrip())
        out = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Return) and isinstance(n.value, ast.Tuple):
                # (value, response_type) — the second element
                second = n.value.elts[1] if len(n.value.elts) > 1 else None
                if isinstance(second, ast.Constant) and isinstance(second.value, str):
                    out.add(second.value)
        return out

    def test_the_producer_emits_only_canonical_values(self):
        emitted = self._producer_response_types()
        assert emitted, "read nothing from the producer — the test is broken"
        unknown = sorted(emitted - set(CANONICAL))
        assert not unknown, (
            f"producer emits {unknown}, which no consumer understands. "
            f"This is #871: such a value reaches the builder's blind-float() "
            f"branch and a coded answer becomes a valueQuantity.")

    def test_CONTROL_the_old_spellings_would_FAIL_that_test(self):
        """Without this, the test above would pass for a vocabulary that
        happened to contain everything."""
        for stale in PRE_871_SPELLINGS:
            assert stale not in CANONICAL, (
                f"{stale!r} is in CANONICAL, so the test above proves nothing")

    def test_the_producer_no_longer_emits_any_of_them(self):
        emitted = self._producer_response_types()
        assert not (emitted & set(PRE_871_SPELLINGS))


class TestNormalise:
    @pytest.mark.parametrize("raw,expect", sorted(ALIASES.items()))
    def test_each_alias_maps_onto_a_canonical_value(self, raw, expect):
        assert normalise(raw) == expect
        assert expect in CANONICAL

    @pytest.mark.parametrize("canon", CANONICAL)
    def test_a_canonical_value_passes_through(self, canon):
        assert normalise(canon) == canon

    @pytest.mark.parametrize("absent", [None, '', '   ', '\t'])
    def test_ABSENCE_is_not_an_error(self, absent):
        """Deliberate. Provider payloads in the wild may omit the field, and a
        vocabulary fix is not the place to start refusing live traffic."""
        assert normalise(absent) == ''

    @pytest.mark.parametrize("bad", ['collect-information', 'Numerical',
                                     'Slider', 'QuestionnaireResponse',
                                     'CATEGORICAL'])
    def test_a_DECLARED_unknown_is_refused_by_name(self, bad):
        with pytest.raises(UnknownResponseType) as ei:
            normalise(bad)
        assert ei.value.raw == bad
        assert bad in str(ei.value)
        # the message must tell the reader what IS accepted
        for c in CANONICAL:
            assert c in str(ei.value)

    def test_an_undeclared_type_is_not_a_survey(self):
        assert not is_survey(normalise(''))


class TestTheCorruptionItself:
    """#489 fixed quantity-coercion of a coded value for the spelling
    `categorical`. These prove the fix now reaches the spelling the producer
    actually used."""

    def test_a_coded_answer_is_a_CODE_not_a_quantity(self):
        obs = build_fhir_observation(
            _row({"value": "120", "response_type": "choice"}))
        assert "valueQuantity" not in obs, (
            "this is the #489 corruption, reached via the #871 spelling")
        assert obs["valueCodeableConcept"]["text"] == "120"

    def test_the_DANGEROUS_case_a_numeric_looking_Likert_code(self):
        """The one that actually corrupts data. A choice whose display reads
        "5" is indistinguishable from a measurement of 5 once it has been
        float()ed into a valueQuantity."""
        obs = build_fhir_observation(
            _row({"value": "5", "response_type": "choice"}))
        assert "valueQuantity" not in obs
        assert obs["valueCodeableConcept"]["coding"][0]["code"] == "5"

    def test_CONTROL_the_coercion_mechanism_is_still_there_for_ABSENCE(self):
        """The control that makes the two tests above mean something: the
        blind-float() branch has not been deleted, it has been made
        unreachable for a DECLARED type. Same value, same mechanism available,
        different outcome — because the type is now understood."""
        obs = build_fhir_observation(_row({"value": "5"}))  # no response_type
        assert obs["valueQuantity"]["value"] == 5.0
        assert "valueCodeableConcept" not in obs

    @pytest.mark.parametrize("spelling", ['datetime', 'date', 'dateTime'])
    def test_a_date_answer_keeps_its_FHIR_TYPE(self, spelling):
        """`datetime` and `date` both landed in `else`: float() failed and the
        value became a valueString, losing the type."""
        obs = build_fhir_observation(
            _row({"value": "2026-03-14", "response_type": spelling}))
        assert obs["valueDateTime"] == "2026-03-14"
        assert "valueString" not in obs


class TestTheCategoryDecision:
    @pytest.mark.parametrize("spelling", ['choice', 'categorical', 'datetime',
                                          'dateTime', 'date', 'boolean',
                                          'text', 'graph'])
    def test_every_answer_type_is_category_SURVEY(self, spelling):
        obs = build_fhir_observation(
            _row({"value": "x", "response_type": spelling}))
        codes = [c["code"] for cat in obs["category"]
                 for c in cat.get("coding", [])]
        assert "survey" in codes, (
            f"{spelling!r} fell to laboratory — before #871 three of these did")

    def test_a_measurement_is_category_LABORATORY(self):
        obs = build_fhir_observation(
            _row({"value": 5, "response_type": "numeric"}))
        codes = [c["code"] for cat in obs["category"]
                 for c in cat.get("coding", [])]
        assert "laboratory" in codes and "survey" not in codes

    def test_an_undeclared_type_stays_LABORATORY(self):
        """Not a change. Documented so the pre-#871 behaviour for absence is
        deliberate rather than overlooked."""
        obs = build_fhir_observation(_row({"value": 5}))
        codes = [c["code"] for cat in obs["category"]
                 for c in cat.get("coding", [])]
        assert "laboratory" in codes


class TestTheCanonicalProjection:
    """`build_canonical_observation` is rosetta's openEHR source (#501)."""

    def test_it_emits_a_canonical_value_outward(self):
        co = build_canonical_observation(
            _row({"value": "120", "response_type": "choice",
                  "concept_guid": "c1"}))
        assert co["response_type"] == "categorical"
        assert co["response_type"] in CANONICAL

    def test_it_does_not_invent_a_type_for_absence(self):
        co = build_canonical_observation(_row({"value": 1, "concept_guid": "c1"}))
        assert co["response_type"] == ''


class TestTheForwarderTreatsItAsTERMINAL:
    """#833: a permanent condition reported as transient tells the operator to
    wait for something that will never change."""

    def test_the_build_refuses_a_declared_unknown(self, monkeypatch):
        log = SimpleNamespace(
            guid="l871", concept_guid="c1", patient_guid="p1",
            service_request_guid=None, contract_guid=None,
            provider_org_guid=None, transaction_guid=None,
            operator_session_id=None, received_at=None,
            fhir_observation_json={"value": 5,
                                   "response_type": "collect-information"})
        with pytest.raises(UnknownResponseType):
            fwd._build_payload(log)

    def test_the_handler_is_ordered_BEFORE_the_generic_one(self):
        """Asserted on the parse tree, not on the source text: a comment
        mentioning UnknownResponseType cannot satisfy this, and the ORDER is
        the whole property — Python takes the first matching handler, so a
        generic `except Exception` placed first would silently route a
        permanent failure into the retry queue."""
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(fwd.run_forwarding_cycle).lstrip())

        # Scope the comparison to the ONE try block that handles it. The
        # function has another try/except Exception around the row claim, so
        # walking the whole function and comparing indices would compare
        # handlers from different try blocks — and would have failed on this
        # correct code. Same shape as CLAUDE.md §7: a pattern applied at a
        # granularity the thing does not live at.
        blocks = [[h.type.id for h in n.handlers if isinstance(h.type, ast.Name)]
                  for n in ast.walk(tree) if isinstance(n, ast.Try)]
        owning = [b for b in blocks if 'UnknownResponseType' in b]
        assert len(owning) == 1, f"expected one owning try block, got {blocks}"
        names = owning[0]
        assert names.index('UnknownResponseType') < names.index('Exception'), (
            f"handler order {names} routes a permanent failure into retries")

    def test_it_marks_failed_and_does_NOT_retry(self, monkeypatch):
        calls = []
        monkeypatch.setattr(fwd, '_mark_failed_terminal',
                            lambda log, msg: calls.append(('terminal', msg)))
        monkeypatch.setattr(fwd, '_mark_retryable',
                            lambda log, msg: calls.append(('retry', msg)))
        monkeypatch.setattr(fwd, '_deliver_one',
                            lambda log: (_ for _ in ()).throw(
                                UnknownResponseType('collect-information')))
        log = SimpleNamespace(guid='l1', patient_guid='p1', attempt_count=0)
        # exercise the handler exactly as the loop does
        try:
            fwd._deliver_one(log)
        except UnknownResponseType as e:
            fwd._mark_failed_terminal(log, str(e))
        assert calls == [('terminal', calls[0][1])]
        assert 'collect-information' in calls[0][1]
        assert not any(k == 'retry' for k, _ in calls)
