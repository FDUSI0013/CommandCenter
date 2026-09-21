"""Record when a password changed, so changing one ends the sessions it opened

Revision ID: 8f3d1c07a2be
Revises: 7b2e4c9a10d3

Session tokens are signed and self-contained: nothing is stored server-side, so
until now nothing could end one early. Changing a password replaced the
credential and left every token minted with the old one valid until it expired
on its own -- which is the opposite of what someone changing a password they
believe is known is trying to achieve.

One nullable timestamp fixes that. It is stamped when a password is *rotated*
(by its owner, or by an admin resetting it), and ``_principal_from_session``
refuses a token issued before it. The user row is already loaded there to check
``is_active``, so the check costs no query.

Null -- every existing account -- refuses nothing, which is right: an account
that has not rotated its credential since this column existed has no session
this was meant to end. The first rotation starts enforcing it.

Nullable and additive, so the previous image reads and writes the table happily
-- but it still cannot BOOT against a database stamped at this revision, because
its own start-up ``alembic upgrade head`` has no script for it. A rollback must
run ``downgrade()`` first; ``deploy/ship.sh`` does that automatically. Dropping
the column loses only the stamps written since the deploy, which is the right
trade: those sessions simply stop being invalidated.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from fulcrum_ops_api.db.base import UtcDateTime

revision: str = "8f3d1c07a2be"
down_revision: str | None = "7b2e4c9a10d3"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("credentials_changed_at", UtcDateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "credentials_changed_at")
