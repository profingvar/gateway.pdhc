"""#871 — the single owner of gateway's response_type vocabulary.

THE DEFECT THIS MODULE CLOSES was not between plan and gateway, which is how
the ticket framed it. It was between gateway's own producer and its own
consumer, in the same repository, measured 2026-11-04:

    producer  _extract_qr_answer       numeric text boolean date datetime choice
    consumer  _SURVEY_TYPES            categorical boolean text dateTime graph
    consumer  _validate_value_type     numeric boolean text categorical dateTime graph

Three disagreed: ``choice`` against ``categorical``, ``datetime`` against
``dateTime`` (a CASE difference), and ``date`` was in no consumer at all.
``text`` and ``boolean`` happened to match, which is why nothing ever showed.

Both consumers were silent in the wrong direction. The validator has no
``else``, so an unrecognised type was validated by nothing. The builder's
``else`` blind-``float()``s, so a coded choice whose display reads numeric — a
Likert "5", a coded "100" — was written to the clinical store as a
``valueQuantity``: a NUMBER where a code belonged. The builder's own docstring
promised "Never blind-float()s a categorical into a quantity" and did exactly
that, because the producer spelled it ``choice``.

So this module exists to make the vocabulary one thing instead of three.

SURVEY_TYPES IS DERIVED, NOT WRITTEN. That is the whole point of the ticket: a
second hand-maintained tuple is what drifted. ``numeric`` is the only canonical
type that denotes a measurement rather than an answer, so the survey set is the
canonical set minus it, and adding a canonical type cannot silently leave the
survey set behind.

ALIASES ARE A BRIDGE, NOT A LICENCE. Every spelling listed there is one
gateway's own code used to emit, kept so that rows already queued in
``cdr_delivery_log`` normalise correctly. The producers now emit canonical
values directly; a NEW spelling belongs in CANONICAL or nowhere.
"""

#: The vocabulary. Each member is a value[x] decision in the builder and a type
#: check in the validator; the two read this tuple rather than repeating it.
CANONICAL = ('numeric', 'boolean', 'text', 'categorical', 'dateTime', 'graph')

#: The one canonical type that denotes a measurement rather than a questionnaire
#: answer. Named so the derivation below reads as the claim it makes.
MEASUREMENT = 'numeric'

#: FHIR ``Observation.category``: survey vs laboratory. Derived — see above.
SURVEY_TYPES = tuple(t for t in CANONICAL if t != MEASUREMENT)

#: Spellings gateway's own producers used before #871. ``date`` maps onto
#: ``dateTime`` because FHIR's dateTime admits a date-only value
#: (``2026-03-14`` is a valid dateTime), so nothing is invented by widening it.
ALIASES = {
    'choice': 'categorical',
    'datetime': 'dateTime',
    'date': 'dateTime',
}


class UnknownResponseType(ValueError):
    """A response_type was DECLARED and names nothing.

    Terminal, never retryable. This is the #833 distinction: a value that
    means nothing will mean nothing on the next attempt too, and telling the
    forwarder to retry would hide a configuration error behind a backoff.

    Absence is a different thing and is NOT this error — see ``normalise``.
    """

    def __init__(self, raw):
        self.raw = raw
        super().__init__(
            f"unknown response_type {raw!r}; expected one of "
            f"{', '.join(CANONICAL)} (aliases: "
            f"{', '.join(sorted(ALIASES))})"
        )


def normalise(raw):
    """Map a declared response_type onto the canonical vocabulary.

    Returns ``''`` for absence — an empty, missing or whitespace-only value.
    Absence is not an error: provider payloads in the wild may omit the field,
    and a vocabulary fix is not the place to start refusing live traffic. The
    callers treat ``''`` as "no declared type" and keep their prior
    best-effort behaviour.

    Raises ``UnknownResponseType`` when a value IS declared and names nothing,
    because silently writing a wrong-typed clinical value is worse than a
    visible terminal failure.
    """
    if raw is None:
        return ''
    s = str(raw).strip()
    if not s:
        return ''
    if s in CANONICAL:
        return s
    if s in ALIASES:
        return ALIASES[s]
    raise UnknownResponseType(s)


def is_survey(canonical_type):
    """True when this canonical type belongs in FHIR category ``survey``.

    Takes a CANONICAL value — call ``normalise`` first. An undeclared type
    (``''``) is not a survey answer and not a measurement; it falls to
    ``laboratory``, which is the behaviour that predates #871.
    """
    return canonical_type in SURVEY_TYPES
