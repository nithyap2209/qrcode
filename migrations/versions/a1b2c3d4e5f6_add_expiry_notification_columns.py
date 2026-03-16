"""Add expiry notification tracking columns to subscribed_users

Revision ID: a1b2c3d4e5f6
Revises: 54fea013d87f
Create Date: 2025-12-16 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = '54fea013d87f'
branch_labels = None
depends_on = None


def upgrade():
    # Add expiry notification tracking columns to subscribed_users table
    with op.batch_alter_table('subscribed_users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('expiry_notification_sent', sa.Boolean(), nullable=True, default=False))
        batch_op.add_column(sa.Column('expiry_notification_sent_at', sa.DateTime(), nullable=True))

    # Set default value for existing rows
    op.execute("UPDATE subscribed_users SET expiry_notification_sent = 0 WHERE expiry_notification_sent IS NULL")


def downgrade():
    # Remove the columns
    with op.batch_alter_table('subscribed_users', schema=None) as batch_op:
        batch_op.drop_column('expiry_notification_sent_at')
        batch_op.drop_column('expiry_notification_sent')
