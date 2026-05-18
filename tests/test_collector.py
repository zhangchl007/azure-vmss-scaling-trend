from __future__ import annotations

from prometheus_client import CollectorRegistry, generate_latest

from vmss_metrics_exporter.collector import VmssMetricsExporter
from vmss_metrics_exporter.models import (
    ManagedLustreCollectionResult,
    ManagedLustreOstMetric,
    VmssCount,
)


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


def test_collect_once_sets_lustre_metrics_and_removes_stale_series() -> None:
    registry = CollectorRegistry()
    vmss_calls = iter([[], []])
    first_lustre = ManagedLustreCollectionResult(
        metrics=(
            ManagedLustreOstMetric(
                "sub-a", "rg-a", "lustre-a", "westus3", "0", 100.0,
                bytes_used=900.0, bytes_total=1000.0,
            ),
            ManagedLustreOstMetric(
                "sub-a", "rg-a", "lustre-b", "westus3", "0", 200.0,
                bytes_used=800.0, bytes_total=1000.0,
            ),
        ),
        filesystem_count=2,
    )
    second_lustre = ManagedLustreCollectionResult(
        metrics=(
            ManagedLustreOstMetric(
                "sub-a", "rg-a", "lustre-a", "westus3", "0", 150.0,
                bytes_used=850.0, bytes_total=1000.0,
            ),
        ),
        filesystem_count=1,
    )
    lustre_calls = iter([first_lustre, second_lustre])
    exporter = VmssMetricsExporter(
        lambda: next(vmss_calls),
        collect_lustre_metrics=lambda: next(lustre_calls),
        registry=registry,
    )

    exporter.collect_once()
    exporter.collect_once()

    metrics = generate_latest(registry).decode()
    expected_labels = (
        'filesystem_name="lustre-a",location="westus3",ostnum="0",'
        'resource_group="rg-a",subscription_id="sub-a"'
    )
    assert f"azure_managed_lustre_ost_bytes_available{{{expected_labels}}} 150.0" in metrics
    assert f"azure_managed_lustre_ost_bytes_used{{{expected_labels}}} 850.0" in metrics
    assert f"azure_managed_lustre_ost_bytes_total{{{expected_labels}}} 1000.0" in metrics
    assert (
        f"azure_managed_lustre_ost_bytes_available_percent{{{expected_labels}}} 15.0"
        in metrics
    )
    assert f"azure_managed_lustre_ost_bytes_used_percent{{{expected_labels}}} 85.0" in metrics
    assert "lustre-b" not in metrics
    assert "azure_managed_lustre_filesystem_total 1.0" in metrics
    assert "azure_managed_lustre_ost_total 1.0" in metrics
    assert "azure_managed_lustre_last_success_timestamp_seconds" in metrics
    assert "azure_managed_lustre_ost_sample_timestamp_seconds" in metrics


def test_lustre_partial_failure_keeps_existing_series() -> None:
    registry = CollectorRegistry()
    first_lustre = ManagedLustreCollectionResult(
        metrics=(
            ManagedLustreOstMetric("sub-a", "rg-a", "lustre-a", "westus3", "0", 100.0),
            ManagedLustreOstMetric(
                "sub-a", "rg-a", "lustre-b", "westus3", "0", 200.0,
                bytes_used=800.0, bytes_total=1000.0,
            ),
        ),
        filesystem_count=2,
    )
    second_lustre = ManagedLustreCollectionResult(
        metrics=(
            ManagedLustreOstMetric("sub-a", "rg-a", "lustre-a", "westus3", "0", 150.0),
        ),
        filesystem_count=2,
        error_count=1,
    )
    lustre_calls = iter([first_lustre, second_lustre])
    exporter = VmssMetricsExporter(
        lambda: [],
        collect_lustre_metrics=lambda: next(lustre_calls),
        registry=registry,
    )

    exporter.collect_lustre_once()
    exporter.collect_lustre_once()

    metrics = generate_latest(registry).decode()
    assert "lustre-a" in metrics
    assert "lustre-b" in metrics
    assert "azure_managed_lustre_collection_errors_total 1.0" in metrics
