"""#1014: cdr_delivery_log.data_origin — patient-generated or NULL.

NULL = produced or endorsed by healthcare (opt-in, operator 2026-11-21).
Nullable, no backfill: no patient app has ever submitted, so every existing
row is healthcare data. Forwarded to cdr as clinical_context.data_origin.

Revision ID: c3d5e7f9a1b4
Revises: b2c3d4e5f6a7
Create Date: 2026-11-21
"""
from alembic import op
import sqlalchemy as sa

revision = 'c3d5e7f9a1b4'
down_revision = 'b2c3d4e5f6a7'
branch_labels = None
depends_on = None


def upgrade():
    insp = sa.inspect(op.get_bind())
    if 'data_origin' not in {c['name'] for c in insp.get_columns('cdr_delivery_log')}:
        op.add_column('cdr_delivery_log',
                      sa.Column('data_origin', sa.String(length=32), nullable=True))


def downgrade():
    insp = sa.inspect(op.get_bind())
    if 'data_origin' in {c['name'] for c in insp.get_columns('cdr_delivery_log')}:
        op.drop_column('cdr_delivery_log', 'data_origin')
