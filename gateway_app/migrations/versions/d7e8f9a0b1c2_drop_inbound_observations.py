"""drop inbound_observations + validation_log tables (#299, SSOT phase 6 closure)

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
Create Date: 2026-06-28 18:30:00.000000

Final step of the SSOT cutover (plans/cdr1_analyse_split_plan.md §10).
After this migration cdr_delivery_log is the sole gateway-side record
of every inbound observation; cdr.pdhc is the sole source of truth
for stored observation data.

Order matters:
1. Drop the FK from cdr_delivery_log.inbound_observation_guid first,
   then the column.
2. Drop the FK from validation_log to inbound_observations.
3. Drop the inbound_observations table.
4. Drop the validation_log table (ObservationValidator's persist
   method was dead code; #299 removed its caller chain).

All four are atomic in one transactional DDL block.
"""
from alembic import op
import sqlalchemy as sa


revision = 'd7e8f9a0b1c2'
down_revision = 'c6d7e8f9a0b1'
branch_labels = None
depends_on = None


def upgrade():
    # 1. cdr_delivery_log: drop FK + the unique CONSTRAINT (not an index)
    # + the column. Postgres distinguishes; dropping the column with
    # the constraint still on it errors.
    op.drop_constraint('fk_cdr_delivery_inbound_obs',
                       'cdr_delivery_log', type_='foreignkey')
    op.drop_constraint('uq_cdr_delivery_inbound_obs',
                       'cdr_delivery_log', type_='unique')
    with op.batch_alter_table('cdr_delivery_log', schema=None) as batch_op:
        batch_op.drop_column('inbound_observation_guid')

    # 2. validation_log: drop FK before dropping the referenced table.
    op.drop_table('validation_log')

    # 3. inbound_observations: gone.
    op.drop_table('inbound_observations')


def downgrade():
    # Best-effort recreation. The original column shapes are restored
    # but the data is gone (downgrade after this point loses every
    # historical inbound row).
    op.create_table(
        'inbound_observations',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('guid', sa.String(36), unique=True, nullable=False),
        sa.Column('service_request_guid', sa.String(36), nullable=False),
        sa.Column('transaction_guid', sa.String(36), nullable=True),
        sa.Column('concept_guid', sa.String(36), nullable=True),
        sa.Column('patient_guid', sa.String(36), nullable=False),
        sa.Column('provider_org_guid', sa.String(36), nullable=False),
        sa.Column('contract_guid', sa.String(36), nullable=False),
        sa.Column('grant_token', sa.String(64), nullable=True),
        sa.Column('fhir_observation_json', sa.JSON, nullable=False),
        sa.Column('value', sa.Text, nullable=True),
        sa.Column('response_type', sa.String(50), nullable=True),
        sa.Column('payload_hash', sa.String(64), nullable=True),
        sa.Column('dedup_key', sa.String(64), nullable=True),
        sa.Column('resolution_status', sa.String(20), nullable=False,
                  server_default='pending'),
        sa.Column('validation_status', sa.String(20), nullable=False,
                  server_default='pending'),
        sa.Column('is_late', sa.Boolean, nullable=False,
                  server_default=sa.text('false')),
        sa.Column('received_at', sa.DateTime(timezone=True)),
        sa.Column('created_at', sa.DateTime(timezone=True)),
    )
    op.create_table(
        'validation_log',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('guid', sa.String(36), unique=True, nullable=False),
        sa.Column('observation_guid', sa.String(36),
                  sa.ForeignKey('inbound_observations.guid'),
                  nullable=False),
        sa.Column('validation_type', sa.String(50), nullable=False),
        sa.Column('passed', sa.Boolean, nullable=False),
        sa.Column('error_details', sa.JSON, nullable=True),
        sa.Column('validated_at', sa.DateTime(timezone=True)),
    )
    with op.batch_alter_table('cdr_delivery_log', schema=None) as batch_op:
        batch_op.add_column(sa.Column('inbound_observation_guid',
                                       sa.String(36), nullable=True))
    op.create_index('uq_cdr_delivery_inbound_obs', 'cdr_delivery_log',
                    ['inbound_observation_guid'], unique=True)
    op.create_foreign_key(
        'fk_cdr_delivery_inbound_obs',
        'cdr_delivery_log', 'inbound_observations',
        ['inbound_observation_guid'], ['guid'],
        ondelete='SET NULL',
    )
