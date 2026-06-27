"""Flask CLI commands for gateway.pdhc.

  flask backfill-cdr-from-inbound   one-shot — enqueue a CdrDeliveryLog
                                    row for every eligible
                                    inbound_observation that lacks one.
                                    Used to seed cdr1 with the historical
                                    7064 rows on first deploy.

  flask recover-failed-cdr          bulk reset status='failed' →
                                    'pending', attempt_count=0. For use
                                    after a cdr1 outage during which the
                                    retry budget was burned.

Both commands are safe to re-run: the upsert via NOT EXISTS / status
filter is idempotent.
"""
import click
from flask import current_app
from sqlalchemy import text
from .extensions import db
from .models import InboundObservation, CdrDeliveryLog


def register_cli(app):
    app.cli.add_command(backfill_cdr_from_inbound)
    app.cli.add_command(recover_failed_cdr)


@click.command('backfill-cdr-from-inbound')
@click.option('--limit', type=int, default=None,
              help='Cap the number of rows enqueued. Useful for a smoke test '
                   'before the full backfill.')
@click.option('--chunk', type=int, default=500,
              help='Commit every N rows.')
def backfill_cdr_from_inbound(limit, chunk):
    """Enqueue forwarder rows for all eligible historical inbound_observations.

    Eligible means: validation_status='valid' AND
    resolution_status='resolved' AND no CdrDeliveryLog row yet.
    Skipped rows (resolution_status='pending' or no concept_guid) get a
    CdrDeliveryLog row with status='skipped' so we know they were seen.
    """
    bind = db.session

    # Eligible (will become status='pending')
    elig_sql = text("""
        SELECT i.guid, i.patient_guid
        FROM inbound_observations i
        LEFT JOIN cdr_delivery_log d
            ON d.inbound_observation_guid = i.guid
        WHERE d.guid IS NULL
          AND i.validation_status = 'valid'
          AND i.resolution_status = 'resolved'
        ORDER BY i.created_at ASC
    """)
    if limit is not None:
        elig_sql = text(str(elig_sql) + f" LIMIT {int(limit)}")

    inserted_pending = 0
    rows_buffer = []
    for row in bind.execute(elig_sql):
        rows_buffer.append({'guid': row.guid, 'patient': row.patient_guid})
        inserted_pending += 1
        if len(rows_buffer) >= chunk:
            _flush(rows_buffer, status='pending')
            rows_buffer = []
            click.echo(f"  enqueued {inserted_pending} pending so far …")
    if rows_buffer:
        _flush(rows_buffer, status='pending')

    # Ineligible — log as skipped so they're not retried later. Skip if
    # the caller capped with --limit (we don't want to mark them all on
    # a smoke run).
    inserted_skipped = 0
    if limit is None:
        skip_sql = text("""
            SELECT i.guid, i.patient_guid
            FROM inbound_observations i
            LEFT JOIN cdr_delivery_log d
                ON d.inbound_observation_guid = i.guid
            WHERE d.guid IS NULL
              AND (i.validation_status <> 'valid'
                   OR i.resolution_status <> 'resolved')
            ORDER BY i.created_at ASC
        """)
        rows_buffer = []
        for row in bind.execute(skip_sql):
            rows_buffer.append({'guid': row.guid, 'patient': row.patient_guid})
            inserted_skipped += 1
            if len(rows_buffer) >= chunk:
                _flush(rows_buffer, status='skipped')
                rows_buffer = []
        if rows_buffer:
            _flush(rows_buffer, status='skipped')

    click.echo(f"Backfill complete: {inserted_pending} enqueued (pending), "
               f"{inserted_skipped} marked skipped.")


def _flush(rows_buffer, status):
    for r in rows_buffer:
        db.session.add(CdrDeliveryLog(
            inbound_observation_guid=r['guid'],
            patient_guid=r['patient'],
            status=status,
        ))
    db.session.commit()


@click.command('recover-failed-cdr')
@click.option('--yes', is_flag=True, help='Confirm. Required.')
def recover_failed_cdr(yes):
    """Reset all 'failed' rows back to 'pending' for retry."""
    failed_count = CdrDeliveryLog.query.filter_by(status='failed').count()
    if failed_count == 0:
        click.echo("No 'failed' rows to recover.")
        return
    if not yes:
        click.echo(f"{failed_count} 'failed' rows would be reset. "
                   "Re-run with --yes to confirm.")
        return
    updated = db.session.execute(text(
        "UPDATE cdr_delivery_log SET status='pending', attempt_count=0, "
        "last_error=NULL, last_attempt_at=NULL WHERE status='failed'"
    )).rowcount
    db.session.commit()
    click.echo(f"Reset {updated} rows from 'failed' to 'pending'.")
