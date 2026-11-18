"""A poll-mode PAT has no push endpoint by design — no warning, no push.

The 'Fix the PAT record' warning fired on every report from cgm_provider,
which is poll-mode (2026-11-18). A push-mode PAT without an endpoint is
still a real misconfiguration and still warns.
"""
import logging
from types import SimpleNamespace

from flask import g

from app.services import report_ingestion as ri

_send = getattr(ri, "_send_receipt", None) or ri.ReportIngestionService._send_receipt


def _call(app, pat):
    with app.test_request_context():
        g.pat_result = pat
        _send("sr-1", "p-1", "org-1", "c-1")


def test_poll_mode_is_quiet(app, caplog):
    with caplog.at_level(logging.WARNING):
        _call(app, SimpleNamespace(delivery_mode="poll", push_endpoint_url=None,
                                   push_secret=None))
    assert not [r for r in caplog.records if "push_endpoint_url" in r.message]


def test_push_mode_without_endpoint_still_warns(app, caplog):
    with caplog.at_level(logging.WARNING):
        _call(app, SimpleNamespace(delivery_mode="push", push_endpoint_url=None,
                                   push_secret=None))
    assert [r for r in caplog.records if "push_endpoint_url" in r.message]
