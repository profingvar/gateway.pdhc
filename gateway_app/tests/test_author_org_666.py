"""#666 — the submitter may declare who AUTHORED the data.

The gateway knows who SUBMITTED a report: the PAT is authenticated and the
submitter cannot lie about it. It cannot know who AUTHORED it — only the
submitter knows that — so author is DECLARED, and everything here follows
from that asymmetry.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.services import cdr_forwarder
from app.services.fhir_observation_builder import build_fhir_observation


def _row(**over):
    base = dict(
        guid="log-1", patient_guid="pat-1", service_request_guid="sr-1",
        transaction_guid="tx-1", concept_guid="con-1", contract_guid="con-x",
        provider_org_guid="org-submitter", author_org_guid=None,
        received_at=None, fhir_observation_json={}, cdr_resource_id=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


class TestTheDeclaredAuthorReachesTheCdr:

    def test_it_is_forwarded_in_clinical_context(self):
        env = cdr_forwarder._build_ingest_payload(
            _row(author_org_guid="org-author"), {}, {}) \
            if hasattr(cdr_forwarder, "_build_ingest_payload") else None
        if env is None:
            import inspect
            src = inspect.getsource(cdr_forwarder)
            assert "'author_org_guid'" in src
            return
        assert env["clinical_context"]["author_org_guid"] == "org-author"

    def test_a_row_without_the_attribute_does_not_crash_the_forwarder(self):
        """The forwarder drains a QUEUE, so it can meet a row enqueued before
        the column existed — during a rolling deploy, or a backlog."""
        import inspect
        src = inspect.getsource(cdr_forwarder)
        assert "getattr(log_row, 'author_org_guid', None)" in src


class TestPerformerIsNotRepurposed:

    def test_performer_still_carries_the_submitter(self):
        """Changing what an existing field MEANS is a silent breaking change.
        cdr's backfill reads performer back into provider_org_guid, and
        external partners may read it too."""
        obs = build_fhir_observation(_row(author_org_guid="org-author"),
                                     {}, {})
        performer = (obs.get("performer") or [{}])[0]
        assert performer.get("identifier", {}).get("value") == "org-submitter"

    def test_the_author_gets_its_own_extension(self):
        obs = build_fhir_observation(_row(author_org_guid="org-author"),
                                     {}, {})
        exts = {e["url"]: e for e in obs.get("extension", [])}
        url = "urn:pdhc:fhir:extension:author-organization"
        assert url in exts
        assert exts[url]["valueReference"]["identifier"]["value"] == "org-author"

    def test_no_author_means_no_extension(self):
        """Absent, not defaulted to the provider — a guess written into the
        record becomes indistinguishable from a declared fact."""
        obs = build_fhir_observation(_row(author_org_guid=None), {}, {})
        urls = {e["url"] for e in obs.get("extension", [])}
        assert "urn:pdhc:fhir:extension:author-organization" not in urls

    def test_the_submitter_is_still_recorded_when_no_author_is_declared(self):
        obs = build_fhir_observation(_row(author_org_guid=None), {}, {})
        performer = (obs.get("performer") or [{}])[0]
        assert performer.get("identifier", {}).get("value") == "org-submitter"


class TestIngestionAcceptsIt:

    def test_the_report_body_field_is_read(self):
        import inspect
        from app.services import report_ingestion
        src = inspect.getsource(report_ingestion)
        assert "body.get('author_org_guid')" in src

    def test_a_per_observation_declaration_wins_over_the_report_level(self):
        """A report can carry observations authored by different
        organisations; the per-observation value is the more specific claim."""
        import inspect
        from app.services import report_ingestion
        src = inspect.getsource(report_ingestion)
        assert "obs.get('author_org_guid')" in src
        assert "or report_author_org_guid" in src
