"""add dedup_key to inbound_observations

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-05-28 11:30:00.000000

Ticket #148: per-observation idempotency on POST /api/v1/provider/report/{sr_guid}.
The dedup key is sha256(patient_guid|transaction_guid|recorded_at). A
re-POST of the same logical observation is now caught at the
per-observation level instead of only via the batch payload_hash
fast-path, so a batch that differs by one obs no longer stores
duplicates of the unchanged ones.

The unique constraint is partial — dedup_key may be NULL when the
observation lacks a recorded_at timestamp, in which case we fall back
to current (no per-obs dedup) behaviour. NULLs are not constrained.
"""
import hashlib

from alembic import op
import sqlalchemy as sa


revision = 'e2f3a4b5c6d7'
down_revision = 'd1e2f3a4b5c6'
branch_labels = None
depends_on = None


def _key(patient_guid, transaction_guid, recorded_at):
    parts = '|'.join([patient_guid or '', transaction_guid or '',
                      recorded_at or ''])
    return hashlib.sha256(parts.encode()).hexdigest()


def upgrade():
    bind = op.get_bind()

    # 1. Add the column (nullable so we can backfill before constraining).
    with op.batch_alter_table('inbound_observations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('dedup_key', sa.String(64), nullable=True))

    # 2. Backfill — compute dedup_key for existing rows that have a
    #    recorded_at value. Rows without recorded_at remain NULL and are
    #    not constrained by the partial unique index.
    rows = bind.execute(sa.text("""
        SELECT id, patient_guid, transaction_guid,
               fhir_observation_json->>'recorded_at' AS recorded_at
        FROM inbound_observations
        WHERE fhir_observation_json->>'recorded_at' IS NOT NULL
    """)).fetchall()
    for row in rows:
        bind.execute(
            sa.text("UPDATE inbound_observations SET dedup_key = :k WHERE id = :id"),
            {'k': _key(row.patient_guid, row.transaction_guid, row.recorded_at),
             'id': row.id},
        )

    # 3. Partial unique index on (service_request_guid, dedup_key) — only
    #    where dedup_key IS NOT NULL. Postgres-specific (the rest of the
    #    pdhc platform runs Postgres; the SQLite test suite uses in-memory
    #    and never exercises this constraint at scale, so the partial
    #    predicate is acceptable to be Postgres-only).
    op.create_index(
        'uq_inbound_obs_sr_dedup_key',
        'inbound_observations',
        ['service_request_guid', 'dedup_key'],
        unique=True,
        postgresql_where=sa.text('dedup_key IS NOT NULL'),
    )


def downgrade():
    op.drop_index('uq_inbound_obs_sr_dedup_key', table_name='inbound_observations')
    with op.batch_alter_table('inbound_observations', schema=None) as batch_op:
        batch_op.drop_column('dedup_key')
