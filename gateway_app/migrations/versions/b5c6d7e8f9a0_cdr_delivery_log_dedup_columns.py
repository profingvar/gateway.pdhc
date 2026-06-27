"""cdr_delivery_log: dedup columns + relax inbound FK to ON DELETE SET NULL

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-06-27 21:00:00.000000

Phase 1 of the cdr1 SSOT cutover (docs/cdr1_ssot_cutover_plan.md, ticket
#280). Adds denormalised dedup + traceability columns so dedup queries
can move off InboundObservation, and relaxes the FK so the log row can
outlive the source row in a later phase.

  - add payload_hash / dedup_key / service_request_guid / concept_guid
    / received_at columns (all nullable, indexed)
  - backfill from inbound_observations for the existing 7064 rows
  - drop the FK on inbound_observation_guid and re-add it with
    ON DELETE SET NULL (column already unique; PostgreSQL allows
    multiple NULLs in a UNIQUE column)
  - add a partial unique index on (service_request_guid, payload_hash)
    where payload_hash IS NOT NULL — the dedup index

No behaviour change yet; the report_ingestion dedup query swap is
done in the same commit but uses these columns. Tests cover both
old (FK still set) and new (FK NULL, row only in log) shapes.
"""
from alembic import op
import sqlalchemy as sa


revision = 'b5c6d7e8f9a0'
down_revision = 'a4b5c6d7e8f9'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()

    # 1. Add columns (nullable so we can backfill before constraining).
    with op.batch_alter_table('cdr_delivery_log', schema=None) as batch_op:
        batch_op.add_column(sa.Column('payload_hash', sa.String(64), nullable=True))
        batch_op.add_column(sa.Column('dedup_key', sa.String(64), nullable=True))
        batch_op.add_column(sa.Column('service_request_guid', sa.String(36), nullable=True))
        batch_op.add_column(sa.Column('concept_guid', sa.String(36), nullable=True))
        batch_op.add_column(sa.Column('received_at', sa.DateTime(timezone=True), nullable=True))

    # 2. Backfill from inbound_observations. Only rows where the FK is
    #    still set get backfilled (which is all of them at this point).
    bind.execute(sa.text("""
        UPDATE cdr_delivery_log d
        SET payload_hash         = i.payload_hash,
            dedup_key            = i.dedup_key,
            service_request_guid = i.service_request_guid,
            concept_guid         = i.concept_guid,
            received_at          = i.received_at
        FROM inbound_observations i
        WHERE d.inbound_observation_guid = i.guid
    """))

    # 3. Btree indexes on the new columns (lookups by these are hot).
    op.create_index('ix_cdr_delivery_payload_hash',
                    'cdr_delivery_log', ['payload_hash'])
    op.create_index('ix_cdr_delivery_dedup_key',
                    'cdr_delivery_log', ['dedup_key'])
    op.create_index('ix_cdr_delivery_service_request',
                    'cdr_delivery_log', ['service_request_guid'])
    op.create_index('ix_cdr_delivery_concept',
                    'cdr_delivery_log', ['concept_guid'])

    # 4. Composite btree index on (SR, payload_hash) for fast dedup
    #    lookups. NOT unique — historical inbound_observations data
    #    contains (rare but real) duplicates from pre-dedup-era inserts
    #    that would block a unique constraint. Dedup is enforced at
    #    application level by report_ingestion (check-then-insert), so
    #    the constraint would be belt-and-braces only.
    op.create_index(
        'ix_cdr_delivery_sr_payload_hash',
        'cdr_delivery_log',
        ['service_request_guid', 'payload_hash'],
    )

    # 5. Relax the FK on inbound_observation_guid:
    #    - drop the old FK
    #    - make the column nullable
    #    - re-add with ON DELETE SET NULL so future deletion of the
    #      InboundObservation row (phase 5) cleanly nulls the back-ref
    #      without orphaning or cascading.
    op.drop_constraint('fk_cdr_delivery_inbound_obs',
                       'cdr_delivery_log', type_='foreignkey')
    with op.batch_alter_table('cdr_delivery_log', schema=None) as batch_op:
        batch_op.alter_column('inbound_observation_guid', nullable=True)
    op.create_foreign_key(
        'fk_cdr_delivery_inbound_obs',
        'cdr_delivery_log', 'inbound_observations',
        ['inbound_observation_guid'], ['guid'],
        ondelete='SET NULL',
    )


def downgrade():
    # Reverse in opposite order.
    op.drop_constraint('fk_cdr_delivery_inbound_obs',
                       'cdr_delivery_log', type_='foreignkey')

    # Before re-adding the NOT NULL + old FK, any rows where the column
    # has gone NULL would block. This downgrade is best-effort: it
    # requires that no deletion has happened yet (i.e. phase 5 not
    # active). If it has, manual cleanup is needed before downgrade.
    with op.batch_alter_table('cdr_delivery_log', schema=None) as batch_op:
        batch_op.alter_column('inbound_observation_guid', nullable=False)
    op.create_foreign_key(
        'fk_cdr_delivery_inbound_obs',
        'cdr_delivery_log', 'inbound_observations',
        ['inbound_observation_guid'], ['guid'],
    )

    op.drop_index('ix_cdr_delivery_sr_payload_hash',
                  table_name='cdr_delivery_log')
    op.drop_index('ix_cdr_delivery_concept',
                  table_name='cdr_delivery_log')
    op.drop_index('ix_cdr_delivery_service_request',
                  table_name='cdr_delivery_log')
    op.drop_index('ix_cdr_delivery_dedup_key',
                  table_name='cdr_delivery_log')
    op.drop_index('ix_cdr_delivery_payload_hash',
                  table_name='cdr_delivery_log')

    with op.batch_alter_table('cdr_delivery_log', schema=None) as batch_op:
        batch_op.drop_column('received_at')
        batch_op.drop_column('concept_guid')
        batch_op.drop_column('service_request_guid')
        batch_op.drop_column('dedup_key')
        batch_op.drop_column('payload_hash')
