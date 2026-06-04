"""Drop observation_vectors table (vectors API removed; per ticket #219).

The /api/v1/vectors/* surface was unauthenticated experimental code with
zero production callers. Dropping the API + service + model + the empty
table that backed them closes a PDL Ch 4 §§ 1-2 exposure (patient data
returned without authentication / audit).

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-06-04
"""
from alembic import op
import sqlalchemy as sa


revision = 'f3a4b5c6d7e8'
down_revision = 'e2f3a4b5c6d7'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('observation_vectors', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_observation_vectors_transaction_guid'))
        batch_op.drop_index(batch_op.f('ix_observation_vectors_plandef_guid'))
        batch_op.drop_index(batch_op.f('ix_observation_vectors_observation_guid'))
        batch_op.drop_index(batch_op.f('ix_observation_vectors_careplan_guid'))
    op.drop_table('observation_vectors')


def downgrade():
    op.create_table(
        'observation_vectors',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('guid', sa.String(length=36), nullable=False),
        sa.Column('observation_guid', sa.String(length=36), nullable=False),
        sa.Column('careplan_guid', sa.String(length=36), nullable=True),
        sa.Column('plandef_guid', sa.String(length=36), nullable=True),
        sa.Column('transaction_guid', sa.String(length=36), nullable=True),
        sa.Column('resolved_context_json', sa.JSON(), nullable=True),
        sa.Column('embedding_json', sa.JSON(), nullable=True),
        sa.Column('vector_model', sa.String(length=100), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['observation_guid'], ['inbound_observations.guid']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('guid'),
    )
    with op.batch_alter_table('observation_vectors', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_observation_vectors_careplan_guid'), ['careplan_guid'], unique=False)
        batch_op.create_index(batch_op.f('ix_observation_vectors_observation_guid'), ['observation_guid'], unique=False)
        batch_op.create_index(batch_op.f('ix_observation_vectors_plandef_guid'), ['plandef_guid'], unique=False)
        batch_op.create_index(batch_op.f('ix_observation_vectors_transaction_guid'), ['transaction_guid'], unique=False)
