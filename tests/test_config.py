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
    assert settings.enable_managed_lustre_metrics is True
    assert settings.lustre_poll_interval_seconds == 60
    assert settings.lustre_metrics_lookback_minutes == 15
    assert settings.lustre_metrics_interval == "PT1M"
    assert settings.lustre_metrics_max_workers == 4


def test_placeholder_subscription_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_SUBSCRIPTION_IDS", "00000000-0000-0000-0000-000000000000")

    with pytest.raises(ValueError, match="AZURE_SUBSCRIPTION_IDS"):
        load_settings()


def test_poll_interval_has_safe_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_SUBSCRIPTION_IDS", "sub-a")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "1")

    with pytest.raises(ValueError, match="POLL_INTERVAL_SECONDS"):
        load_settings()


def test_lustre_settings_can_be_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_SUBSCRIPTION_IDS", "sub-a")
    monkeypatch.setenv("ENABLE_MANAGED_LUSTRE_METRICS", "false")
    monkeypatch.setenv("LUSTRE_POLL_INTERVAL_SECONDS", "30")
    monkeypatch.setenv("LUSTRE_METRICS_LOOKBACK_MINUTES", "30")
    monkeypatch.setenv("LUSTRE_METRICS_INTERVAL", "PT5M")
    monkeypatch.setenv("LUSTRE_METRICS_MAX_WORKERS", "8")

    settings = load_settings()

    assert settings.enable_managed_lustre_metrics is False
    assert settings.lustre_poll_interval_seconds == 30
    assert settings.lustre_metrics_lookback_minutes == 30
    assert settings.lustre_metrics_interval == "PT5M"
    assert settings.lustre_metrics_max_workers == 8


def test_lustre_boolean_setting_rejects_invalid_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_SUBSCRIPTION_IDS", "sub-a")
    monkeypatch.setenv("ENABLE_MANAGED_LUSTRE_METRICS", "maybe")

    with pytest.raises(ValueError, match="ENABLE_MANAGED_LUSTRE_METRICS"):
        load_settings()
