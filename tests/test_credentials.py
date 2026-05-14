"""Tests for the resilient credential fallback chain."""

from __future__ import annotations

import os
from collections.abc import Iterable

import pytest
from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import CredentialUnavailableError

from vmss_metrics_exporter import credentials as creds_module
from vmss_metrics_exporter.credentials import ResilientAzureCredential


class _FakeCred:
    """Test double that mimics a TokenCredential."""

    def __init__(self, behavior: Iterable[object]) -> None:
        # behavior is an iterable of either AccessToken instances or Exception instances.
        # Each get_token call consumes the next item.
        self._iter = iter(list(behavior))
        self.calls = 0

    def get_token(self, *_scopes: str, **_kwargs: object) -> AccessToken:
        self.calls += 1
        outcome = next(self._iter)
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, AccessToken)
        return outcome

    def close(self) -> None:  # pragma: no cover - close is best-effort.
        pass


def _install_chain(monkeypatch: pytest.MonkeyPatch, chain: list[tuple[str, object]]) -> None:
    monkeypatch.setattr(creds_module, "_build_credential_chain", lambda: chain)


def test_workload_identity_success_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    """If WI returns a token, MI must never be called."""

    token = AccessToken("wi-token", expires_on=9_999_999_999)
    wi = _FakeCred([token])
    mi = _FakeCred([Exception("must not be called")])
    _install_chain(monkeypatch, [("workload-identity", wi), ("managed-identity", mi)])

    cred = ResilientAzureCredential()
    result = cred.get_token("https://management.azure.com/.default")

    assert result.token == "wi-token"
    assert wi.calls == 1
    assert mi.calls == 0


def test_falls_back_to_managed_identity_on_hard_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ClientAuthenticationError` from WI must NOT abort the chain — this is the bug fix."""

    wi = _FakeCred(
        [
            ClientAuthenticationError(
                message="AADSTS700211: No matching federated identity record found."
            )
        ]
    )
    mi_token = AccessToken("mi-token", expires_on=9_999_999_999)
    mi = _FakeCred([mi_token])
    _install_chain(monkeypatch, [("workload-identity", wi), ("managed-identity", mi)])

    cred = ResilientAzureCredential()
    result = cred.get_token("https://management.azure.com/.default")

    assert result.token == "mi-token"
    assert wi.calls == 1
    assert mi.calls == 1


def test_falls_back_on_credential_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    wi = _FakeCred([CredentialUnavailableError(message="WI env vars not present")])
    mi_token = AccessToken("mi-token", expires_on=9_999_999_999)
    mi = _FakeCred([mi_token])
    _install_chain(monkeypatch, [("workload-identity", wi), ("managed-identity", mi)])

    cred = ResilientAzureCredential()
    result = cred.get_token("https://management.azure.com/.default")

    assert result.token == "mi-token"


def test_all_failures_raise_combined_error(monkeypatch: pytest.MonkeyPatch) -> None:
    wi = _FakeCred([ClientAuthenticationError(message="AADSTS700211")])
    mi = _FakeCred([ClientAuthenticationError(message="IMDS unreachable")])
    _install_chain(monkeypatch, [("workload-identity", wi), ("managed-identity", mi)])

    cred = ResilientAzureCredential()
    with pytest.raises(ClientAuthenticationError) as excinfo:
        cred.get_token("https://management.azure.com/.default")

    message = str(excinfo.value)
    assert "workload-identity" in message
    assert "managed-identity" in message
    assert "AADSTS700211" in message
    assert "IMDS unreachable" in message


def test_active_credential_cached_then_invalidated_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once a credential succeeds it's reused; if it later fails the chain is re-run."""

    token1 = AccessToken("wi-token-1", expires_on=9_999_999_999)
    token2 = AccessToken("mi-token-2", expires_on=9_999_999_999)
    wi = _FakeCred(
        [
            token1,
            ClientAuthenticationError(message="WI suddenly broken"),
            ClientAuthenticationError(message="WI still broken"),
        ]
    )
    mi = _FakeCred([token2])
    _install_chain(monkeypatch, [("workload-identity", wi), ("managed-identity", mi)])

    cred = ResilientAzureCredential()

    first = cred.get_token("scope/.default")
    assert first.token == "wi-token-1"
    # Cached: a second call goes straight to wi without walking the chain. That call
    # fails, so the chain is re-walked: wi is tried again (and fails again), then mi.
    second = cred.get_token("scope/.default")
    assert second.token == "mi-token-2"
    assert wi.calls == 3  # 1 cached success + 1 cached-call failure + 1 re-walk failure
    assert mi.calls == 1


def test_construction_requires_at_least_one_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_chain(monkeypatch, [])
    with pytest.raises(RuntimeError):
        ResilientAzureCredential()


def test_chain_skips_workload_identity_when_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WorkloadIdentityCredential must be omitted when WI env vars are not all present."""

    monkeypatch.delenv("AZURE_FEDERATED_TOKEN_FILE", raising=False)
    monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
    monkeypatch.delenv("AZURE_TENANT_ID", raising=False)

    chain = creds_module._build_credential_chain()
    names = [name for name, _ in chain]
    assert not any(name.startswith("workload-identity") for name in names)
    # Managed Identity (system-assigned) and DefaultAzureCredential are always added.
    assert any(name.startswith("managed-identity(system-assigned)") for name in names)
    assert "default-azure-credential" in names


def test_chain_includes_workload_identity_when_env_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_CLIENT_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setenv("AZURE_TENANT_ID", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", "/var/run/secrets/azure/tokens/azure-identity-token")

    chain = creds_module._build_credential_chain()
    names = [name for name, _ in chain]
    assert names[0] == "workload-identity"
    # User-assigned MI is included because AZURE_CLIENT_ID is set.
    assert any(name.startswith("managed-identity(client_id=") for name in names)


def test_force_imds_managed_identity_clears_then_restores_token_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The MI construction context manager must not leak env-var changes."""

    monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", "/var/run/secrets/token")

    with creds_module._force_imds_managed_identity():
        assert "AZURE_FEDERATED_TOKEN_FILE" not in os.environ

    # After the context exits the env var is back, so WorkloadIdentityCredential keeps working.
    assert os.environ["AZURE_FEDERATED_TOKEN_FILE"] == "/var/run/secrets/token"
