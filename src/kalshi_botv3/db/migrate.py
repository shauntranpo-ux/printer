import asyncio
from pathlib import Path

_ALEMBIC_INI = Path(__file__).parents[3] / "alembic.ini"


def _upgrade_head() -> None:
    from alembic.config import Config

    from alembic import command

    cfg = Config(str(_ALEMBIC_INI))
    command.upgrade(cfg, "head")


async def run_migrations() -> None:
    """Run `alembic upgrade head` from an async context."""
    await asyncio.to_thread(_upgrade_head)
