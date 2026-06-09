"""Ticket #224 — receipt_token -> resource_guid rename.

Pins the new column name + index + to_dict shape so a future
revert can't slip through without an explicit migration.
"""
from __future__ import annotations

import pytest
from sqlalchemy import inspect

from app.extensions import db
from app.models import AuditLog


def test_audit_log_has_resource_guid_column(client, db):
    """The model exposes resource_guid and NOT receipt_token."""
    cols = {c.name for c in AuditLog.__table__.columns}
    assert "resource_guid" in cols
    assert "receipt_token" not in cols


def test_db_table_has_resource_guid_column(client, db):
    """The runtime DB matches the model after the #224 migration."""
    insp = inspect(db.engine)
    names = {c["name"] for c in insp.get_columns("audit_log")}
    assert "resource_guid" in names
    assert "receipt_token" not in names


def test_audit_log_resource_guid_indexed(client, db):
    """The index is created under the new name so kontroller queries
    by primary resource stay cheap."""
    insp = inspect(db.engine)
    indexes = insp.get_indexes("audit_log")
    names = {ix["name"] for ix in indexes}
    assert "ix_audit_log_resource_guid" in names
    assert "ix_audit_log_receipt_token" not in names


def test_to_dict_carries_resource_guid(client, db):
    """The JSON shape exposed by /audit and friends matches the new
    column name."""
    row = AuditLog(
        event_type="resource_guid.test",
        actor_guid="actor-x",
        resource_guid="res-y",
    )
    db.session.add(row)
    db.session.commit()
    payload = row.to_dict()
    assert payload["resource_guid"] == "res-y"
    assert "receipt_token" not in payload


def test_filter_by_resource_guid(client, db):
    """Round-trip: write resource_guid, read it back via the renamed
    column, ensure the index is exercisable."""
    db.session.add(AuditLog(
        event_type="resource_guid.roundtrip",
        actor_guid="actor-r",
        resource_guid="res-z-1",
    ))
    db.session.add(AuditLog(
        event_type="resource_guid.roundtrip",
        actor_guid="actor-r",
        resource_guid="res-z-2",
    ))
    db.session.commit()
    hits = AuditLog.query.filter_by(resource_guid="res-z-1").all()
    assert len(hits) == 1
    assert hits[0].resource_guid == "res-z-1"
