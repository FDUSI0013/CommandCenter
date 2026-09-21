"""Make four service-level rules true in the schema, and index two hot reads

Revision ID: 7b2e4c9a10d3
Revises: 51168850ae0f

Four things the services already intend, and one the Policy Center reads, were
only ever enforced in application code:

* a configuration has exactly one current revision,
* a user holds at most one active seat per licence,
* an issue has at most one open backlog item,
* a deduplicated condition has at most one unresolved alert.

Each is checked inside a transaction, which is enough until two workers do it at
once: both read, both see nothing, both insert. The services then behave oddly
in ways that look like bugs elsewhere -- a configuration with two current
revisions can be neither rolled back nor re-imported, and ``raise_alert``
already catches the IntegrityError and folds onto the twin, so for alerts this
index *is* the fix.

A partial unique index says it once, for every writer, for good. Both engines
honour one: SQLite has since 3.8, and the model definitions carry both
``sqlite_where`` and ``postgresql_where`` so the schema a test builds from
``create_all`` is the schema this produces.

Existing rows are repaired first, because a unique index will not build over a
duplicate -- and a refusal here is the evidence that the constraint was needed.
The repairs are written to be no-ops where the data is already sound.

The two plain indexes serve reads that had none:

* ``policy_violations (policy_id, occurred_at)`` -- the 30-day rollup the
  scheduler recomputes counts per policy over a window, and the inspector's
  Violations tab reads the same shape. On ``policy_id`` alone that is a scan of
  everything a busy policy ever wrote (114,555 rows when this was measured on
  2026-09-19, and growing).
* ``audit_events (workspace_id, entity_id, occurred_at)`` -- "history for this
  record", which is always within one workspace and newest first. Neither the
  ``(entity_type, entity_id)`` nor the ``(workspace_id, occurred_at)`` index can
  serve that on its own.

Rolling this back means running its ``downgrade()``, not merely putting the
previous image back. The control plane migrates itself at start
(``alembic upgrade head && exec uvicorn ...``) from scripts baked into its image,
so an image that predates this revision cannot resolve the revision the database
would report: it exits 255 before uvicorn and, under ``restart: unless-stopped``,
loops. ``deploy/ship.sh`` handles that -- its ``restore()`` downgrades to the
revision recorded before the deploy while the new image is still the tagged one.
Once the schema is back, the previous image reads and writes exactly as before.

No version of the old image ever runs against this schema: ``restore()``
downgrades first, and only then puts the old image back. Were one to -- someone
starting it by hand against a database at this revision -- it would behave
normally except in one narrow case: code that would have written a duplicate
gets an IntegrityError where it used to get silent corruption. ``raise_alert``
already handles that; the other three paths would surface a 500 on a write that
was already wrong.

Neither is built CONCURRENTLY: both tables are small enough that the ordinary
lock is measured in milliseconds, and CONCURRENTLY cannot run inside the
transaction Alembic wraps a migration in. On a deployment where these tables
have grown to millions of rows, build them by hand outside this migration.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "7b2e4c9a10d3"
down_revision: str | None = "51168850ae0f"
branch_labels: str | None = None
depends_on: str | None = None

#: (index, table, columns, predicate). The predicate is SQL both engines parse.
UNIQUE_INDEXES: list[tuple[str, str, list[str], str]] = [
    (
        "uq_configuration_versions_current",
        "configuration_versions",
        ["configuration_id"],
        "is_current",
    ),
    (
        "uq_seat_assignments_active",
        "seat_assignments",
        ["license_id", "user_id"],
        "released_at IS NULL",
    ),
    (
        "uq_backlog_items_open_issue",
        "backlog_items",
        ["issue_id"],
        "issue_id IS NOT NULL AND status <> 'Done'",
    ),
    (
        "uq_alerts_open_dedupe",
        "alerts",
        ["workspace_id", "dedupe_key"],
        "dedupe_key IS NOT NULL AND status <> 'Resolved'",
    ),
]

READ_INDEXES: list[tuple[str, str, list[str]]] = [
    ("ix_policy_violations_policy_occurred", "policy_violations", ["policy_id", "occurred_at"]),
    (
        "ix_audit_events_ws_entity_time",
        "audit_events",
        ["workspace_id", "entity_id", "occurred_at"],
    ),
]


def _repair() -> None:
    """Resolve any existing row that would refuse the indexes below.

    Every statement keeps the row the service itself would have returned, so a
    repair changes what the database *permits*, never what the product shows.
    """
    # The survivor is whichever row ``_current_version`` already serves -- most
    # recently published, then most recently created, then highest id. Ranking by
    # ``configurations.current_version`` instead would be defensible on its own
    # terms, but it would pick a DIFFERENT row from the one the product is
    # serving today, and a migration that silently swaps a governance object's
    # live body is not a repair.
    #
    # The denormalised pointer is moved onto that row first, while the groups
    # that have one are still identifiable. ``_publish`` writes the flag and the
    # pointer in the same unit of work, so a disagreement between them is itself
    # a symptom of the race this index closes; only configurations that actually
    # carry the race are touched.
    op.execute(
        sa.text(
            """
            UPDATE configurations SET current_version = (
                    SELECT v.version FROM configuration_versions v
                     WHERE v.configuration_id = configurations.id AND v.is_current
                     ORDER BY v.published_at DESC NULLS LAST,
                              v.created_at DESC,
                              v.id DESC
                     LIMIT 1)
             WHERE id IN (
                     SELECT configuration_id FROM configuration_versions
                      WHERE is_current
                      GROUP BY configuration_id HAVING COUNT(*) > 1
                   )
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE configuration_versions
               SET is_current = FALSE,
                   -- Paired with the demotion, exactly as ``_publish`` pairs
                   -- them. Clearing the flag alone leaves status='Active' on a
                   -- row that is not current -- a combination no code path can
                   -- produce, which the Versions modal would then render as a
                   -- second active revision.
                   status = CASE WHEN status = 'Active' THEN 'Deprecated' ELSE status END
             WHERE is_current
               AND id NOT IN (
                     SELECT id FROM (
                       SELECT id, ROW_NUMBER() OVER (
                                PARTITION BY configuration_id
                                ORDER BY published_at DESC NULLS LAST,
                                         created_at DESC,
                                         id DESC
                              ) AS rank
                         FROM configuration_versions WHERE is_current
                     ) ranked WHERE rank = 1
                   )
            """
        )
    )
    # Of several active seats for one user, the earliest is the one they hold;
    # the rest are releases that were never recorded.
    op.execute(
        sa.text(
            """
            UPDATE seat_assignments SET released_at = assigned_at
             WHERE released_at IS NULL
               AND id NOT IN (
                     SELECT id FROM (
                       SELECT id, ROW_NUMBER() OVER (
                                PARTITION BY license_id, user_id
                                ORDER BY assigned_at ASC, id ASC
                              ) AS rank
                         FROM seat_assignments WHERE released_at IS NULL
                     ) ranked WHERE rank = 1
                   )
            """
        )
    )
    # ``seats_assigned`` is a stored count, recomputed only inside assign and
    # release. Releasing a duplicate above without recomputing it would leave the
    # licence claiming a seat nobody holds -- and the workspace summary, which
    # counts live rows, disagreeing with the licence row that reads the counter.
    # Written as a full reconciliation rather than a decrement: it is a no-op
    # wherever the two already agree, and repairs any earlier drift for free.
    op.execute(
        sa.text(
            """
            UPDATE tenant_licenses SET seats_assigned = (
                    SELECT COUNT(*) FROM seat_assignments s
                     WHERE s.license_id = tenant_licenses.id AND s.released_at IS NULL)
             WHERE seats_assigned <> (
                    SELECT COUNT(*) FROM seat_assignments s
                     WHERE s.license_id = tenant_licenses.id AND s.released_at IS NULL)
            """
        )
    )
    # The oldest open item is the one services.feedback returns for an issue;
    # the others are detached rather than deleted, so no work is lost.
    op.execute(
        sa.text(
            """
            UPDATE backlog_items SET issue_id = NULL
             WHERE issue_id IS NOT NULL AND status <> 'Done'
               AND id NOT IN (
                     SELECT id FROM (
                       SELECT id, ROW_NUMBER() OVER (
                                PARTITION BY issue_id ORDER BY created_at ASC, id ASC
                              ) AS rank
                         FROM backlog_items
                        WHERE issue_id IS NOT NULL AND status <> 'Done'
                     ) ranked WHERE rank = 1
                   )
            """
        )
    )
    # Duplicate raises of one condition fold onto the NEWEST live alert, because
    # that is the row ``_live_alert`` returns (``raised_at DESC LIMIT 1``) and so
    # the row every recurrence has been landing on -- the one at the top of the
    # operator's list, whose occurrence_count has been climbing. Keeping the
    # oldest instead would move the count back to a row nobody is watching.
    op.execute(
        sa.text(
            """
            UPDATE alerts SET dedupe_key = NULL
             WHERE dedupe_key IS NOT NULL AND status <> 'Resolved'
               AND id NOT IN (
                     SELECT id FROM (
                       SELECT id, ROW_NUMBER() OVER (
                                PARTITION BY workspace_id, dedupe_key
                                ORDER BY raised_at DESC, id DESC
                              ) AS rank
                         FROM alerts
                        WHERE dedupe_key IS NOT NULL AND status <> 'Resolved'
                     ) ranked WHERE rank = 1
                   )
            """
        )
    )


def upgrade() -> None:
    _repair()
    dialect = op.get_bind().dialect.name
    where = "postgresql_where" if dialect == "postgresql" else "sqlite_where"
    for name, table, columns, predicate in UNIQUE_INDEXES:
        op.create_index(
            name, table, columns, unique=True, **{where: sa.text(predicate)}
        )
    for name, table, columns in READ_INDEXES:
        op.create_index(name, table, columns)


def downgrade() -> None:
    for name, table, _columns in READ_INDEXES:
        op.drop_index(name, table_name=table)
    for name, table, _columns, _predicate in UNIQUE_INDEXES:
        op.drop_index(name, table_name=table)
    # The repairs are not undone: they resolved rows that the product already
    # treated as resolved, and re-duplicating them would serve nobody.
