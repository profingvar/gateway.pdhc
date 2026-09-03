"""Flask CLI commands for gateway.pdhc.

  flask recover-failed-cdr   bulk reset CdrDeliveryLog status='failed'
                             -> 'pending', attempt_count=0. For use
                             after a cdr1 outage during which the
                             retry budget was burned.

The historical backfill-cdr-from-inbound + delete-already-delivered
commands were removed in #299 (the inbound_observations table is
gone; cdr_delivery_log is the sole queue).
"""
import click
from sqlalchemy import text
from .extensions import db
from .models import CdrDeliveryLog
from .services.sr_context import SRContextService
from .services.patient_sync import ensure_cdr_patient


def register_cli(app):
    app.cli.add_command(recover_failed_cdr)
    app.cli.add_command(backfill_cdr_patients)


@click.command('backfill-cdr-patients')
@click.option('--yes', is_flag=True,
              help='Actually write to cdr1. Without it, dry-run only.')
def backfill_cdr_patients(yes):
    """Backfill CDR1 patient demographics for patients that already have data.

    For each distinct patient in cdr_delivery_log, resolve demographics from ips
    (via request.pdhc's SR context) and upsert a Patient into cdr1 so the
    care-delivery patient list shows a name. Idempotent; patients ips doesn't
    know are skipped and stay pseudonymous.
    """
    rows = (db.session.query(CdrDeliveryLog.patient_guid,
                             CdrDeliveryLog.service_request_guid)
            .filter(CdrDeliveryLog.service_request_guid.isnot(None))
            .all())
    sr_by_patient = {}
    for patient_guid, sr_guid in rows:
        if patient_guid and sr_guid:
            sr_by_patient.setdefault(patient_guid, sr_guid)
    if not sr_by_patient:
        click.echo('No patients with a service_request_guid in cdr_delivery_log.')
        return

    written = skipped = 0
    for patient_guid, sr_guid in sr_by_patient.items():
        ctx = SRContextService.fetch(sr_guid)
        name = ctx.patient_name if ctx.found else ''
        if not name:
            skipped += 1
            click.echo(f'  skip  {patient_guid[:12]}  (no ips demographics)')
            continue
        if not yes:
            written += 1
            click.echo(f'  would-write  {patient_guid[:12]}  name={name!r}')
            continue
        ok = ensure_cdr_patient(patient_guid, name, ctx.patient_birth_date,
                                cache=False)
        written += 1 if ok else 0
        skipped += 0 if ok else 1
        click.echo(f'  {"wrote" if ok else "FAILED"}  {patient_guid[:12]}  '
                   f'name={name!r}')

    verb = 'wrote' if yes else 'would write'
    click.echo(f'{verb} {written}, skipped {skipped} '
               f'(of {len(sr_by_patient)} patients).')


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
