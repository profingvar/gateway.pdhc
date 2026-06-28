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


def register_cli(app):
    app.cli.add_command(recover_failed_cdr)


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
