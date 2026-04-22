import pytest

from kalshi_botv3.utils.logging import configure_logging


@pytest.fixture(scope="session", autouse=True)
def setup_logging() -> None:
    configure_logging("INFO")
