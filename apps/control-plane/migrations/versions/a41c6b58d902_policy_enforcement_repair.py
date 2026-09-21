"""Set each policy's enforcement column to the mode it is actually enforcing

Revision ID: a41c6b58d902
Revises: 8f3d1c07a2be

A policy carries its enforcement twice: ``policies.enforcement`` is the column
the Policy Center shows and the ``?enforcement=`` filter selects on, and
``rules.action.mode`` inside the rule body is what the enforcement path reads.
Writes have kept the two equal since the validation in
``services/policies._agreed_enforcement`` went in, and reads already prefer the
rule body (``schemas/policies.enforced_mode``) -- but a row written before that
can still show one word and do another, and the filter, which only sees the
column, then hands a reviewer the wrong list.

This copies the rule body's mode into the column wherever the two disagree and
the body names one. It changes what is *displayed and filtered*, never what is
enforced: the column was already the ignored half.

Rows whose body names no mode are left alone -- there the column is the answer,
and ``enforced_mode`` falls back to it.

There is no schema change, and no behaviour depends on the repair having run, so
the previous image reads this data exactly as it did before. It still cannot boot
against a database stamped at this revision -- start-up runs
``alembic upgrade head`` against scripts baked into the image -- so a rollback
runs ``downgrade()`` first, which ``deploy/ship.sh`` does automatically. That
downgrade is a no-op by design: the value this replaced was the one being
ignored, and putting it back would serve nobody.

Written twice because the two engines extract from JSON differently: Postgres
with ``->``/``->>``, SQLite with ``json_extract``. Neither statement is run
against the other's database.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "a41c6b58d902"
down_revision: str | None = "8f3d1c07a2be"
branch_labels: str | None = None
depends_on: str | None = None

POSTGRESQL = """
    UPDATE policies
       SET enforcement = rules -> 'action' ->> 'mode'
     WHERE rules -> 'action' ->> 'mode' IS NOT NULL
       AND rules -> 'action' ->> 'mode' <> ''
       AND rules -> 'action' ->> 'mode' <> enforcement
"""

SQLITE = """
    UPDATE policies
       SET enforcement = json_extract(rules, '$.action.mode')
     WHERE json_valid(rules)
       AND json_extract(rules, '$.action.mode') IS NOT NULL
       AND json_extract(rules, '$.action.mode') <> ''
       AND json_extract(rules, '$.action.mode') <> enforcement
"""


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(sa.text(POSTGRESQL))
    elif dialect == "sqlite":
        op.execute(sa.text(SQLITE))
    # Any other engine: the column stays as written. Reads already resolve the
    # rule body first, so nothing is enforced incorrectly -- the filter is
    # simply not repaired, which is what it was before this revision.


def downgrade() -> None:
    """Nothing to undo: the previous value was the one being ignored."""
