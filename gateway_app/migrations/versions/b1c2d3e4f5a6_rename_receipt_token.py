"""audit_log: rename receipt_token -> resource_guid (ticket #224)

Revision ID: b1c2d3e4f5a6
Revises: a7b8c9d0e1f2
Create Date: 2026-06-09 11:30:00.000000

The legacy ``receipt_token`` column on ``audit_log`` was the field every
event_type used to record its primary resource guid — but it's only a
"receipt token" in the original /provider/receipt/<token>/ack call site.
For observations.read it holds the organisation guid; for
bundle.downloaded / report.received it holds the ServiceRequest guid;
for push.delivered it holds the dispatch receipt guid. The single field
shape doesn't change — only the name.

Renaming to ``resource_guid`` to align with the cross-service PDHC
convention (ips.pdhc AuditLog.resource_guid and request.pdhc
AuditLog.resource_guid both use this name for the same concept). The
underlying VARCHAR(255) and nullable + indexed shape stay identical.

Drops + recreates the index so the new name picks up
``ix_audit_log_resource_guid`` consistently across Alembic-managed
environments.
"""
from alembic import op
import sqlalchemy as sa


revision = 'b1c2d3e4f5a6'
down_revision = 'a7b8c9d0e1f2'
branch_labels = None
depends_on = None


def upgrade():
    # Drop the old index, rename the column, recreate the index under
    # the new name. batch_alter_table doesn't auto-rename indices, so
    # spell it out for portability.
    op.drop_index('ix_audit_log_receipt_token', table_name='audit_log')
    with op.batch_alter_table('audit_log', schema=None) as batch_op:
        batch_op.alter_column(
            'receipt_token',
            new_column_name='resource_guid',
            existing_type=sa.String(length=255),
            existing_nullable=True,
        )
    op.create_index(
        'ix_audit_log_resource_guid',
        'audit_log', ['resource_guid'],
        unique=False,
    )


def downgrade():
    op.drop_index('ix_audit_log_resource_guid', table_name='audit_log')
    with op.batch_alter_table('audit_log', schema=None) as batch_op:
        batch_op.alter_column(
            'resource_guid',
            new_column_name='receipt_token',
            existing_type=sa.String(length=255),
            existing_nullable=True,
        )
    op.create_index(
        'ix_audit_log_receipt_token',
        'audit_log', ['receipt_token'],
        unique=False,
    )
