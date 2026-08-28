"""add_numero_to_consommations

Revision ID: 838f5deb17e8
Revises: 
Create Date: 2026-07-29 18:59:02.341229

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '838f5deb17e8'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    op.add_column('consommations',
        sa.Column('numero', sa.String(), nullable=True)
    )
    op.execute("UPDATE consommations SET numero = 'INCONNU' WHERE numero IS NULL")
    op.alter_column('consommations', 'numero', nullable=False)
    op.create_index('ix_consommations_numero', 'consommations', ['numero'])


def downgrade() -> None:
    op.drop_index('ix_consommations_numero', table_name='consommations')
    op.drop_column('consommations', 'numero')