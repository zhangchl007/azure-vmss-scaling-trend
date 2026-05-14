"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

_PLACEHOLDER_SUBSCRIPTION = "00000000-0000-0000-0000-000000000000"


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings for the VMSS metrics exporter."""

    subscription_ids: tuple[str, ...]
    poll_interval_seconds: int = 300
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
    arg_page_size: int = 1000
    arg_max_retries: int = 3
    arg_retry_base_delay_seconds: float = 1.0


def load_settings(*, require_subscription_ids: bool = True) -> Settings:
    """Load exporter settings from `.env` and process environment.

    `DefaultAzureCredential` reads Azure auth-related environment variables itself, so this module
    only parses exporter-specific settings.
    """

    load_dotenv()

    subscription_ids = _parse_subscription_ids(os.getenv("AZURE_SUBSCRIPTION_IDS", ""))
    if require_subscription_ids and not subscription_ids:
        raise ValueError(
            "AZURE_SUBSCRIPTION_IDS must contain at least one real Azure subscription ID. "
            "Use a comma-separated list for multiple subscriptions."
        )

    return Settings(
        subscription_ids=subscription_ids,
        poll_interval_seconds=_get_int("POLL_INTERVAL_SECONDS", default=300, minimum=30),
        host=os.getenv("HOST", "0.0.0.0"),
        port=_get_int("PORT", default=8000, minimum=1, maximum=65535),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        arg_page_size=_get_int("ARG_PAGE_SIZE", default=1000, minimum=1, maximum=1000),
        arg_max_retries=_get_int("ARG_MAX_RETRIES", default=3, minimum=0, maximum=10),
        arg_retry_base_delay_seconds=_get_float(
            "ARG_RETRY_BASE_DELAY_SECONDS", default=1.0, minimum=0.0, maximum=60.0
        ),
    )


def _parse_subscription_ids(raw: str) -> tuple[str, ...]:
    values = tuple(
        item.strip()
        for item in raw.replace(";", ",").split(",")
        if item.strip() and item.strip() != _PLACEHOLDER_SUBSCRIPTION
    )
    return tuple(dict.fromkeys(values))


def _get_int(
    name: str,
    *,
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}, got {value}")
    return value


def _get_float(
    name: str,
    *,
    default: float,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}, got {value}")
    return value
