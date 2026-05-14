from __future__ import annotations

from prometheus_client import CollectorRegistry, generate_latest

from vmss_metrics_exporter.collector import VmssMetricsExporter
from vmss_metrics_exporter.models import VmssCount


def test_collect_once_sets_metrics_and_removes_stale_series() -> None:
    registry = CollectorRegistry()
    first = [
        VmssCount(
            "sub-a", "rg-a", "vmss-a", "eastus", "Uniform", 3, 5,
            vm_size="Standard_D2s_v3", sku_tier="Standard",
        ),
        VmssCount(
            "sub-a", "rg-a", "vmss-b", "eastus", "Flexible", 1, 2,
            vm_size="Standard_D4s_v5", sku_tier="Standard",
        ),
    ]
    second = [
        VmssCount(
            "sub-a", "rg-a", "vmss-a", "eastus", "Uniform", 4, 6,
            vm_size="Standard_D8s_v5", sku_tier="Standard",
        ),
    ]
    calls = iter([first, second])
    exporter = VmssMetricsExporter(lambda: next(calls), registry=registry)

    exporter.collect_once()
    exporter.collect_once()

    metrics = generate_latest(registry).decode()
    expected_labels = (
        'location="eastus",orchestration_mode="Uniform",resource_group="rg-a",'
        'subscription_id="sub-a",vmss_name="vmss-a"'
    )
    assert f"azure_vmss_instance_count{{{expected_labels}}} 4.0" in metrics
    assert f"azure_vmss_capacity{{{expected_labels}}} 6.0" in metrics
    # New info metric reflects the latest sku.name and is stale-cleaned across reloads.
    assert (
        'azure_vmss_info{location="eastus",orchestration_mode="Uniform",'
        'resource_group="rg-a",sku_tier="Standard",subscription_id="sub-a",'
        'vm_size="Standard_D8s_v5",vmss_name="vmss-a"} 1.0'
    ) in metrics
    assert "vmss-b" not in metrics
    assert 'vm_size="Standard_D2s_v3"' not in metrics
    assert "azure_vmss_exporter_vmss_total 1.0" in metrics
