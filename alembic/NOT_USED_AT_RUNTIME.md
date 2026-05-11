# QUARANTINED — Not Used at Runtime

This directory contains Alembic migrations for the **abandoned rewrite** ("kalshi-botv3").

- These migrations target a SQLAlchemy schema that is **never created** by the live bot.
- The live bot uses **raw sqlite3** via `bot_infra.init_db()` (see `bot_infra.py:158`). No ORM. No migrations.
- `alembic upgrade head` will **not** work with the live bot's database.

**Do not run Alembic commands against the production database.**
**Do not edit these migrations unless you are intentionally reviving the rewrite.**

See [`ARCHITECTURE.md`](../ARCHITECTURE.md) for the authoritative layout.
