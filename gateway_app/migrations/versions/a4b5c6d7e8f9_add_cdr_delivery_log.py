"""add cdr_delivery_log

Revision ID: a4b5c6d7e8f9
Revises: f3a4b5c6d7e8
Create Date: 2026-06-27 14:00:00.000000

Adds the producer-side delivery log that tracks forwarding of
inbound_observations rows to cdr.pdhc (cdr1). Same insert-then-send
pattern as cdr.pdhc/cdr_app/app/services/cambio_worker.py (its outbound
sender to real Cambio) — one row per inbound observation, polled by
APScheduler-driven worker, status pending → delivered | failed |
skipped.
"""
from alembic import op
import sqlalchemy as sa


revision = 'a4b5c6d7e8f9'
down_revision = 'b1c2d3e4f5a6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'cdr_delivery_log',
        sa.Column('guid', sa.String(36), primary_key=True),
        sa.Column('inbound_observation_guid', sa.String(36), nullable=False),
        sa.Column('patient_guid', sa.String(36), nullable=False),
        sa.Column('cdr_resource_id', sa.String(128), nullable=True),
        sa.Column('status', sa.String(32), nullable=False,
                  server_default='pending'),
        sa.Column('attempt_count', sa.Integer, nullable=False,
                  server_default='0'),
        sa.Column('last_attempt_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text, nullable=True),
        sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.ForeignKeyConstraint(
            ['inbound_observation_guid'], ['inbound_observations.guid'],
            name='fk_cdr_delivery_inbound_obs',
        ),
        sa.UniqueConstraint(
            'inbound_observation_guid',
            name='uq_cdr_delivery_inbound_obs',
        ),
    )
    op.create_index(
        'ix_cdr_delivery_status_created',
        'cdr_delivery_log',
        ['status', 'created_at'],
    )
    op.create_index(
        'ix_cdr_delivery_patient',
        'cdr_delivery_log',
        ['patient_guid'],
    )


def downgrade():
    op.drop_index('ix_cdr_delivery_patient', table_name='cdr_delivery_log')
    op.drop_index('ix_cdr_delivery_status_created', table_name='cdr_delivery_log')
    op.drop_table('cdr_delivery_log')
