"""Resilient Azure credential chain with Workload Identity → Managed Identity fallback.

Background
----------
`azure.identity.DefaultAzureCredential` chains several credentials but only proceeds to
the next one when the current one raises `CredentialUnavailableError`. A real
authentication failure (for example AADSTS70021 / AADSTS700211 federated-credential
mismatch, AADSTS53003 conditional-access block, or a malformed federated token file)
raises `ClientAuthenticationError`, which **stops the chain**.

In practice, on AKS the Workload Identity admission webhook injects WI environment
variables. `DefaultAzureCredential` then commits to `WorkloadIdentityCredential` and if
WI auth fails it never falls back to `ManagedIdentityCredential` — even though the
kubelet/user-assigned identity attached to the underlying VMSS could have served the
token via IMDS.

This module provides `ResilientAzureCredential`, which:

1. Tries `WorkloadIdentityCredential` first when WI env vars are present.
2. On any failure (including hard auth errors), falls back to
   `ManagedIdentityCredential` — user-assigned via `AZURE_CLIENT_ID` if set, then
   system-assigned.
3. Falls back to `DefaultAzureCredential` (with WI/MI explicitly disabled) for local
   development (Azure CLI, environment, etc.).
4. Caches the first credential that succeeds so subsequent token requests reuse it
   without re-running the chain.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any

from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import (
    CredentialUnavailableError,
    DefaultAzureCredential,
    ManagedIdentityCredential,
    WorkloadIdentityCredential,
)

LOGGER = logging.getLogger(__name__)

_WORKLOAD_IDENTITY_ENV_VARS = (
    "AZURE_CLIENT_ID",
    "AZURE_TENANT_ID",
    "AZURE_FEDERATED_TOKEN_FILE",
)
# Env vars that make `ManagedIdentityCredential` short-circuit into the Workload
# Identity token-exchange flow. We need to clear these while constructing MI so the
# fallback actually targets IMDS.
_MI_TO_WI_TRIGGER_ENV_VARS = ("AZURE_FEDERATED_TOKEN_FILE",)


class ResilientAzureCredential:
    """Token credential that falls back from Workload Identity to Managed Identity.

    The standard `DefaultAzureCredential` / `ChainedTokenCredential` only falls through
    on `CredentialUnavailableError`. This implementation catches *any* exception from
    a credential and tries the next one, then surfaces a single combined error if every
    credential fails.
    """

    def __init__(self, *, scopes_for_probe: tuple[str, ...] | None = None) -> None:
        self._lock = threading.Lock()
        self._active: tuple[str, Any] | None = None
        self._credentials = _build_credential_chain()
        self._scopes_for_probe = scopes_for_probe
        if not self._credentials:
            raise RuntimeError(
                "No Azure credentials could be initialized. Ensure azure-identity is "
                "installed and at least one auth mechanism (workload identity, managed "
                "identity, environment, or Azure CLI) is configured."
            )

    # The signature mirrors `TokenCredential.get_token` so this object is a drop-in.
    def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        # Fast path: a credential has already proven itself.
        with self._lock:
            active = self._active
        if active is not None:
            name, cred = active
            try:
                return cred.get_token(*scopes, **kwargs)
            except Exception as exc:  # noqa: BLE001 - intentional: re-run the chain on any error.
                LOGGER.warning(
                    "Cached credential %s failed (%s); re-running fallback chain.",
                    name,
                    _summarize_error(exc),
                )
                with self._lock:
                    self._active = None

        errors: list[str] = []
        for name, cred in self._credentials:
            try:
                token = cred.get_token(*scopes, **kwargs)
            except CredentialUnavailableError as exc:
                LOGGER.debug("Credential %s unavailable: %s", name, exc)
                errors.append(f"{name}: unavailable ({_summarize_error(exc)})")
                continue
            except Exception as exc:  # noqa: BLE001 - keep walking the chain on auth errors.
                LOGGER.warning(
                    "Credential %s failed (%s); trying next credential in fallback chain.",
                    name,
                    _summarize_error(exc),
                )
                errors.append(f"{name}: {_summarize_error(exc)}")
                continue

            LOGGER.info("Acquired Azure token via %s", name)
            with self._lock:
                self._active = (name, cred)
            return token

        raise ClientAuthenticationError(
            "All Azure credentials in the fallback chain failed:\n  - "
            + "\n  - ".join(errors)
        )

    def close(self) -> None:
        for _, cred in self._credentials:
            with suppress(Exception):
                close = getattr(cred, "close", None)
                if callable(close):
                    close()


def _build_credential_chain() -> list[tuple[str, Any]]:
    """Build the ordered list of credentials to try."""

    chain: list[tuple[str, Any]] = []
    client_id = os.getenv("AZURE_CLIENT_ID")
    has_workload_identity_env = all(os.getenv(name) for name in _WORKLOAD_IDENTITY_ENV_VARS)

    if has_workload_identity_env:
        try:
            chain.append(("workload-identity", WorkloadIdentityCredential()))
        except Exception as exc:  # noqa: BLE001 - construction itself may raise on bad config.
            LOGGER.warning(
                "WorkloadIdentityCredential could not be constructed: %s", _summarize_error(exc)
            )
    else:
        LOGGER.debug(
            "Workload Identity env vars not all present; skipping WorkloadIdentityCredential."
        )

    if client_id:
        try:
            with _force_imds_managed_identity():
                cred = ManagedIdentityCredential(client_id=client_id)
            chain.append(
                (
                    f"managed-identity(client_id={_short_uuid(client_id)})",
                    cred,
                )
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "User-assigned ManagedIdentityCredential could not be constructed: %s",
                _summarize_error(exc),
            )

    try:
        with _force_imds_managed_identity():
            cred = ManagedIdentityCredential()
        chain.append(("managed-identity(system-assigned)", cred))
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning(
            "System-assigned ManagedIdentityCredential could not be constructed: %s",
            _summarize_error(exc),
        )

    try:
        chain.append(
            (
                "default-azure-credential",
                DefaultAzureCredential(
                    exclude_workload_identity_credential=True,
                    exclude_managed_identity_credential=True,
                ),
            )
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning(
            "DefaultAzureCredential could not be constructed: %s", _summarize_error(exc)
        )

    return chain


def _summarize_error(exc: BaseException) -> str:
    """Return a single-line summary of an exception suitable for logs."""

    message = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    return f"{exc.__class__.__name__}: {message[:240]}"


def _short_uuid(value: str) -> str:
    """Shorten a UUID for logs while keeping the prefix recognizable."""

    return f"{value[:8]}…" if len(value) > 8 else value


@contextmanager
def _force_imds_managed_identity() -> Iterator[None]:
    """Temporarily clear env vars that make `ManagedIdentityCredential` use Workload Identity.

    In modern `azure-identity`, `ManagedIdentityCredential` detects WI env vars at
    construction time and internally uses the WI token-exchange flow instead of IMDS.
    For our fallback chain to provide a *real* alternative when WI is broken, MI must
    target IMDS directly. We clear the trigger env vars only for the duration of MI
    construction, then restore them so `WorkloadIdentityCredential` still works.
    """

    saved: dict[str, str] = {}
    for key in _MI_TO_WI_TRIGGER_ENV_VARS:
        value = os.environ.pop(key, None)
        if value is not None:
            saved[key] = value
    try:
        yield
    finally:
        os.environ.update(saved)


def create_credential() -> ResilientAzureCredential:
    """Public entry point used by the rest of the package."""

    return ResilientAzureCredential()
