from __future__ import annotations

import pytest

from vmss_metrics_exporter.config import load_settings


def test_load_settings_parses_subscriptions_and_deduplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_SUBSCRIPTION_IDS", "sub-a, sub-b;sub-a")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "300")
    monkeypatch.setenv("PORT", "9000")

    settings = load_settings()

    assert settings.subscription_ids == ("sub-a", "sub-b")
    assert settings.poll_interval_seconds == 300
    assert settings.port == 9000


def test_placeholder_subscription_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_SUBSCRIPTION_IDS", "00000000-0000-0000-0000-000000000000")

    with pytest.raises(ValueError, match="AZURE_SUBSCRIPTION_IDS"):
        load_settings()


def test_poll_interval_has_safe_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_SUBSCRIPTION_IDS", "sub-a")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "1")

    with pytest.raises(ValueError, match="POLL_INTERVAL_SECONDS"):
        load_settings()
