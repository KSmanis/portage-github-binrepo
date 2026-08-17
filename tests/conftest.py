import portage
import pytest


@pytest.fixture(autouse=True)
def disable_portage_legacy_globals() -> None:
    """Prevent Portage from loading host configuration during tests."""
    portage._disable_legacy_globals()
