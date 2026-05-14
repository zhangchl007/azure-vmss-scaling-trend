from __future__ import annotations

from dataclasses import dataclass

import pytest

from vmss_metrics_exporter.azure_resource_graph import (
    VMSS_COUNTS_QUERY,
    AzureResourceGraphVmssCollector,
    normalize_vmss_count_row,
    parse_vmss_parent_from_child_id,
    summarize_counts,
)


@dataclass
class FakeResponse:
    data: list[dict[str, object]]
    skip_token: str | None = None


class FakeResourceGraphClient:
    def __init__(self) -> None:
        self.calls = 0

    def resources(self, _query: object) -> FakeResponse:
        self.calls += 1
        return FakeResponse(
            [
                {
                    "subscriptionId": "sub-a",
                    "resourceGroup": "rg-a",
                    "vmssName": "vmss-a",
                    "location": "eastus",
                    "orchestrationMode": "Uniform",
                    "vmSize": "Standard_D2s_v3",
                    "skuTier": "Standard",
                    "actualInstanceCount": 3,
                    "capacity": 5,
                }
            ]
        )


class FakeBadRequestClient:
    def __init__(self) -> None:
        self.calls = 0

    def resources(self, _query: object) -> object:
        self.calls += 1
        response = type("Response", (), {"status_code": 400})()
        raise FakeAzureError(response)


class FakeAzureError(Exception):
    def __init__(self, response: object) -> None:
        super().__init__("bad request")
        self.response = response


def test_normalize_vmss_count_row() -> None:
    count = normalize_vmss_count_row(
        {
            "subscriptionId": "sub-a",
            "resourceGroup": "rg-a",
            "vmssName": "vmss-a",
            "location": "eastus",
            "orchestrationMode": "Flexible",
            "vmSize": "Standard_D4s_v5",
            "skuTier": "Standard",
            "actualInstanceCount": "2",
            "capacity": "4",
        }
    )

    assert count.subscription_id == "sub-a"
    assert count.actual_instance_count == 2
    assert count.capacity == 4
    assert count.vm_size == "Standard_D4s_v5"
    assert count.sku_tier == "Standard"
    assert count.label_values == ("sub-a", "rg-a", "vmss-a", "eastus", "Flexible")
    assert count.info_label_values == (
        "sub-a",
        "rg-a",
        "vmss-a",
        "eastus",
        "Flexible",
        "Standard_D4s_v5",
        "Standard",
    )


def test_normalize_vmss_count_row_defaults_when_sku_missing() -> None:
    count = normalize_vmss_count_row(
        {
            "subscriptionId": "sub-a",
            "resourceGroup": "rg-a",
            "vmssName": "vmss-a",
        }
    )

    assert count.vm_size == "unknown"
    assert count.sku_tier == "unknown"


def test_vmss_counts_query_avoids_unsupported_let_statements() -> None:
    assert "let " not in VMSS_COUNTS_QUERY.lower()
    assert "ComputeResources" in VMSS_COUNTS_QUERY
    assert "microsoft.compute/virtualmachinescalesets/virtualmachines" in VMSS_COUNTS_QUERY
    assert "microsoft.compute/virtualmachines'" in VMSS_COUNTS_QUERY
    assert "vmSize = tostring(sku.name)" in VMSS_COUNTS_QUERY
    assert "skuTier = tostring(sku.tier)" in VMSS_COUNTS_QUERY


def test_parse_vmss_parent_from_child_id() -> None:
    resource_id = (
        "/subscriptions/sub-a/resourceGroups/rg-a/providers/Microsoft.Compute/"
        "virtualMachineScaleSets/vmss-a/virtualMachines/12"
    )

    assert parse_vmss_parent_from_child_id(resource_id) == ("sub-a", "rg-a", "vmss-a")


def test_parse_vmss_parent_rejects_non_child_id() -> None:
    with pytest.raises(ValueError):
        parse_vmss_parent_from_child_id("/subscriptions/sub-a/resourceGroups/rg-a")


def test_collector_normalizes_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "vmss_metrics_exporter.azure_resource_graph.build_query_request",
        lambda **kwargs: kwargs,
    )
    client = FakeResourceGraphClient()
    collector = AzureResourceGraphVmssCollector(client, ["sub-a"], retry_base_delay_seconds=0)

    counts = collector.collect()

    assert client.calls == 1
    assert len(counts) == 1
    assert counts[0].vmss_name == "vmss-a"
    assert counts[0].actual_instance_count == 3


def test_collector_does_not_retry_bad_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "vmss_metrics_exporter.azure_resource_graph.build_query_request",
        lambda **kwargs: kwargs,
    )
    client = FakeBadRequestClient()
    collector = AzureResourceGraphVmssCollector(
        client,
        ["sub-a"],
        max_retries=3,
        retry_base_delay_seconds=0,
    )

    with pytest.raises(FakeAzureError):
        collector.collect()

    assert client.calls == 1


def test_summarize_counts_contains_tabular_output() -> None:
    row = normalize_vmss_count_row(
        {
            "subscriptionId": "sub-a",
            "resourceGroup": "rg-a",
            "vmssName": "vmss-a",
            "location": "eastus",
            "orchestrationMode": "Uniform",
            "vmSize": "Standard_DS2_v2",
            "skuTier": "Standard",
            "actualInstanceCount": 1,
            "capacity": 1,
        }
    )

    summary = summarize_counts([row])

    assert "subscription_id" in summary
    assert "vm_size" in summary
    assert "sku_tier" in summary
    assert "vmss-a" in summary
    assert "Standard_DS2_v2" in summary
