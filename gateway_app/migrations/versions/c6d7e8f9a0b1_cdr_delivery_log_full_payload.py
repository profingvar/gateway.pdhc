"""cdr_delivery_log: absorb inbound_observations payload (#285 prep)

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-06-28 16:30:00.000000

SSOT phase 6 (#285) finalises the cutover: after this migration plus
the consumer refactors, inbound_observations becomes droppable.

Adds to cdr_delivery_log everything the forwarder + the cross-SR
aggregators (contract_scope.py, request_completion.py) need to read
WITHOUT joining inbound_observations:

  - transaction_guid          per-observation activity guid
  - contract_guid             provider contract reference
  - provider_org_guid         performer / org
  - fhir_observation_json     full FHIR R5 Observation resource
                              (the heavy column the forwarder builds
                              the wire payload from)

All nullable for now; the next migration (#285 step 5) drops the
inbound_observations FK and the table itself. Backfill walks
inbound_observations and populates the new columns on rows that
still have an inbound twin (FK set). Rows whose FK is already NULL
(SSOT phase 5 deletion run) keep the new columns NULL — they were
already delivered, so the forwarder won't reread them.
"""
from alembic import op
import sqlalchemy as sa


revision = 'c6d7e8f9a0b1'
down_revision = 'b5c6d7e8f9a0'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()

    with op.batch_alter_table('cdr_delivery_log', schema=None) as batch_op:
        batch_op.add_column(sa.Column('transaction_guid', sa.String(36), nullable=True))
        batch_op.add_column(sa.Column('contract_guid', sa.String(36), nullable=True))
        batch_op.add_column(sa.Column('provider_org_guid', sa.String(36), nullable=True))
        batch_op.add_column(sa.Column('fhir_observation_json', sa.JSON, nullable=True))

    op.create_index('ix_cdr_delivery_transaction',
                    'cdr_delivery_log', ['transaction_guid'])

    # Backfill from inbound_observations for rows that still have an FK.
    # Rows with NULL FK (deleted in phase 5) are already delivered and
    # the forwarder won't read them again.
    bind.execute(sa.text("""
        UPDATE cdr_delivery_log d
        SET transaction_guid      = i.transaction_guid,
            contract_guid         = i.contract_guid,
            provider_org_guid     = i.provider_org_guid,
            fhir_observation_json = i.fhir_observation_json
        FROM inbound_observations i
        WHERE d.inbound_observation_guid = i.guid
    """))


def downgrade():
    op.drop_index('ix_cdr_delivery_transaction', table_name='cdr_delivery_log')
    with op.batch_alter_table('cdr_delivery_log', schema=None) as batch_op:
        batch_op.drop_column('fhir_observation_json')
        batch_op.drop_column('provider_org_guid')
        batch_op.drop_column('contract_guid')
        batch_op.drop_column('transaction_guid')
