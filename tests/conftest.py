"""Tests never read the developer's .env file; each test passes the settings it needs."""

import pytest

from tesis_payment_provider.config import Settings


@pytest.fixture(autouse=True)
def ignore_local_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(Settings.model_config, "env_file", None)
