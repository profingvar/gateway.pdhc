"""cdr_delivery_log: add operator_session_id (X2 #408)

Captures the operator session (SSO `sid` / X-Operator-Session-Id) at ingest so
the async cdr forwarder can replay it to cdr1 and the Lag (2022:913)
chain-of-custody survives the request→queue→delivery gap.

Nullable — legacy rows and machine-to-machine pushes with no operator session
stay NULL (the receiver's "NULL = no operator correlation" contract).

Revision ID: e9f0a1b2c3d4
Revises: d7e8f9a0b1c2
Create Date: 2026-07-04
"""
from alembic import op
import sqlalchemy as sa

revision = 'e9f0a1b2c3d4'
down_revision = 'd7e8f9a0b1c2'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'cdr_delivery_log',
        sa.Column('operator_session_id', sa.String(length=128), nullable=True),
    )


def downgrade():
    op.drop_column('cdr_delivery_log', 'operator_session_id')
