"""Home Assistant tests use generated credentials and mocked external services."""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow HA to load the integration from this checkout for every test."""
    return
