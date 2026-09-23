"""#666: add cdr_delivery_log.author_org_guid — who CREATED an observation.

The submitter MAY declare which organisation authored the data, when that is
not itself. Optional: every existing integration keeps working unchanged and
simply leaves this NULL.

NO BACKFILL, deliberately, matching cdr #665. Defaulting it to
provider_org_guid would be a guess, and once written a guess is
indistinguishable from a value the submitter actually declared. NULL means
"the submitter did not say", which stays honest.

Note on the asymmetry, repeated here because it is the point of the field:
provider_org_guid is AUTHENTICATED — it comes from the PAT and the submitter
cannot falsify it. author_org_guid can only be DECLARED, because only the
submitter knows who authored. Anything that filters or groups on it is
trusting the submitter's word.

Revision ID: b2c3d4e5f6a7
Revises: e9f0a1b2c3d4
Create Date: 2026-09-23
"""
from alembic import op
import sqlalchemy as sa

revision = "b2c3d4e5f6a7"
down_revision = "e9f0a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("cdr_delivery_log",
                  sa.Column("author_org_guid", sa.String(length=36),
                            nullable=True))
    op.create_index("ix_cdr_delivery_log_author_org_guid",
                    "cdr_delivery_log", ["author_org_guid"])


def downgrade():
    op.drop_index("ix_cdr_delivery_log_author_org_guid",
                  table_name="cdr_delivery_log")
    op.drop_column("cdr_delivery_log", "author_org_guid")
