from __future__ import annotations

from dataclasses import dataclass

import pytest

from vmss_metrics_exporter.azure_managed_lustre import (
    AMLFS_FILESYSTEMS_QUERY,
    OST_CAPACITY_METRICS,
    AzureManagedLustreCollector,
    normalize_filesystem_row,
    normalize_ost_bytes_available_response,
    normalize_ost_capacity_response,
    parse_iso_duration,
    summarize_lustre_metrics,
)
from vmss_metrics_exporter.models import (
    ManagedLustreCollectionResult,
    ManagedLustreFilesystem,
    ManagedLustreOstMetric,
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
                    "id": "/subscriptions/sub-a/resourceGroups/rg-a/providers/"
                    "Microsoft.StorageCache/amlFilesystems/lustre-a",
                    "subscriptionId": "sub-a",
                    "resourceGroup": "rg-a",
                    "filesystemName": "lustre-a",
                    "location": "westus3",
                    "skuTier": "AMLFS-Durable-Premium-500",
                    "storageCapacityTiB": "8",
                }
            ]
        )


class FakeMetricsClient:
    def __init__(self) -> None:
        self.resource_uris: list[str] = []
        self.metric_names: list[object] = []

    def query_resource(self, resource_uri: str, metric_names: object, **_kwargs: object) -> object:
        self.resource_uris.append(resource_uri)
        self.metric_names.append(metric_names)
        return {
            "metrics": [
                {
                    "name": "OSTBytesAvailable",
                    "timeseries": [
                        {
                            "metadata_values": [
                                {"name": {"value": "ostnum"}, "value": "0"},
                            ],
                            "data": [
                                {"average": None},
                                {"average": 123.0},
                            ],
                        },
                    ],
                },
                {
                    "name": "OSTBytesUsed",
                    "timeseries": [
                        {
                            "metadata_values": [
                                {"name": {"value": "ostnum"}, "value": "0"},
                            ],
                            "data": [
                                {"average": 877.0},
                            ],
                        },
                    ],
                },
                {
                    "name": "OSTBytesTotal",
                    "timeseries": [
                        {
                            "metadata_values": [
                                {"name": {"value": "ostnum"}, "value": "0"},
                            ],
                            "data": [
                                {"average": 1000.0},
                            ],
                        },
                    ],
                }
            ]
        }


class PartiallyFailingMetricsClient(FakeMetricsClient):
    def query_resource(self, resource_uri: str, _metric_names: object, **_kwargs: object) -> object:
        if resource_uri.endswith("lustre-b"):
            raise RuntimeError("boom")
        return super().query_resource(resource_uri, _metric_names, **_kwargs)


def test_amlfs_query_discovers_all_filesystems() -> None:
    assert "microsoft.storagecache/amlfilesystems" in AMLFS_FILESYSTEMS_QUERY.lower()
    assert "filesystemName = name" in AMLFS_FILESYSTEMS_QUERY
    assert "storageCapacityTiB" in AMLFS_FILESYSTEMS_QUERY
    assert "let " not in AMLFS_FILESYSTEMS_QUERY.lower()


def test_normalize_filesystem_row() -> None:
    filesystem = normalize_filesystem_row(
        {
            "id": "/subscriptions/sub-a/resourceGroups/rg-a/providers/"
            "Microsoft.StorageCache/amlFilesystems/lustre-a",
            "subscriptionId": "sub-a",
            "resourceGroup": "rg-a",
            "filesystemName": "lustre-a",
            "location": "westus3",
            "skuTier": "AMLFS-Durable-Premium-500",
            "storageCapacityTiB": "8",
        }
    )

    assert filesystem.subscription_id == "sub-a"
    assert filesystem.filesystem_name == "lustre-a"
    assert filesystem.location == "westus3"
    assert filesystem.storage_capacity_tib == 8.0


def test_normalize_ost_bytes_available_response_uses_latest_non_null_average() -> None:
    filesystem = ManagedLustreFilesystem(
        "sub-a",
        "rg-a",
        "lustre-a",
        "/subscriptions/sub-a/resourceGroups/rg-a/providers/"
        "Microsoft.StorageCache/amlFilesystems/lustre-a",
        "westus3",
    )

    metrics = normalize_ost_bytes_available_response(
        filesystem,
        {
            "metrics": [
                {
                    "name": "OSTBytesAvailable",
                    "timeseries": [
                        {
                            "metadata_values": [
                                {"name": {"value": "ostnum"}, "value": "1"},
                            ],
                            "data": [
                                {"average": 10.0},
                                {"average": None},
                                {"average": 42.0},
                            ],
                        },
                        {
                            "metadata_values": [
                                {"name": {"value": "other"}, "value": "ignored"},
                            ],
                            "data": [{"average": 99.0}],
                        },
                    ],
                }
            ]
        },
    )

    assert len(metrics) == 1
    assert metrics[0].label_values == ("sub-a", "rg-a", "lustre-a", "westus3", "1")
    assert metrics[0].bytes_available == 42.0


def test_normalize_ost_capacity_response_groups_metric_names_by_ostnum() -> None:
    filesystem = ManagedLustreFilesystem(
        "sub-a",
        "rg-a",
        "lustre-a",
        "/subscriptions/sub-a/resourceGroups/rg-a/providers/"
        "Microsoft.StorageCache/amlFilesystems/lustre-a",
        "westus3",
    )

    metrics = normalize_ost_capacity_response(
        filesystem,
        {
            "metrics": [
                {
                    "name": "OSTBytesTotal",
                    "timeseries": [
                        {
                            "metadata_values": [
                                {"name": {"value": "ostnum"}, "value": "1"},
                            ],
                            "data": [{"average": 1000.0}],
                        }
                    ],
                },
                {
                    "name": "OSTBytesAvailable",
                    "timeseries": [
                        {
                            "metadata_values": [
                                {"name": {"value": "ostnum"}, "value": "1"},
                            ],
                            "data": [{"average": 250.0}],
                        }
                    ],
                },
                {
                    "name": "OSTBytesUsed",
                    "timeseries": [
                        {
                            "metadata_values": [
                                {"name": {"value": "ostnum"}, "value": "1"},
                            ],
                            "data": [{"average": 750.0}],
                        }
                    ],
                },
            ]
        },
    )

    assert len(metrics) == 1
    assert metrics[0].bytes_available == 250.0
    assert metrics[0].bytes_used == 750.0
    assert metrics[0].bytes_total == 1000.0
    assert metrics[0].bytes_available_percent == 25.0
    assert metrics[0].bytes_used_percent == 75.0


def test_collector_discovers_and_collects_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "vmss_metrics_exporter.azure_managed_lustre.build_query_request",
        lambda **kwargs: kwargs,
    )
    rg_client = FakeResourceGraphClient()
    metrics_client = FakeMetricsClient()
    collector = AzureManagedLustreCollector(
        rg_client,
        metrics_client,
        ["sub-a"],
        retry_base_delay_seconds=0,
    )

    result = collector.collect()

    assert rg_client.calls == 1
    assert result.filesystem_count == 1
    assert result.error_count == 0
    assert len(result.metrics) == 1
    assert result.metrics[0].ostnum == "0"
    assert result.metrics[0].bytes_available == 123.0
    assert result.metrics[0].bytes_used == 877.0
    assert result.metrics[0].bytes_total == 1000.0
    assert metrics_client.metric_names == [OST_CAPACITY_METRICS]


def test_collector_isolates_per_filesystem_metric_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "vmss_metrics_exporter.azure_managed_lustre.build_query_request",
        lambda **kwargs: kwargs,
    )

    class TwoFilesystemResourceGraphClient:
        def resources(self, _query: object) -> FakeResponse:
            return FakeResponse(
                [
                    {
                        "id": "/subscriptions/sub-a/resourceGroups/rg-a/providers/"
                        "Microsoft.StorageCache/amlFilesystems/lustre-a",
                        "subscriptionId": "sub-a",
                        "resourceGroup": "rg-a",
                        "filesystemName": "lustre-a",
                        "location": "westus3",
                    },
                    {
                        "id": "/subscriptions/sub-a/resourceGroups/rg-a/providers/"
                        "Microsoft.StorageCache/amlFilesystems/lustre-b",
                        "subscriptionId": "sub-a",
                        "resourceGroup": "rg-a",
                        "filesystemName": "lustre-b",
                        "location": "westus3",
                    },
                ]
            )

    collector = AzureManagedLustreCollector(
        TwoFilesystemResourceGraphClient(),
        PartiallyFailingMetricsClient(),
        ["sub-a"],
        retry_base_delay_seconds=0,
    )

    result = collector.collect()

    assert result.filesystem_count == 2
    assert result.error_count == 1
    assert len(result.metrics) == 1
    assert result.metrics[0].filesystem_name == "lustre-a"


def test_parse_iso_duration_accepts_monitor_intervals() -> None:
    assert parse_iso_duration("PT1M").total_seconds() == 60
    assert parse_iso_duration("PT1H").total_seconds() == 3600


@pytest.mark.parametrize("value", ["", "P1D", "PT0M", "5m"])
def test_parse_iso_duration_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError):
        parse_iso_duration(value)


def test_summarize_lustre_metrics_empty() -> None:
    summary = summarize_lustre_metrics(
        ManagedLustreCollectionResult(
            metrics=(
                ManagedLustreOstMetric("sub-a", "rg-a", "lustre-a", "westus3", "0", 123.0),
            ),
            filesystem_count=1,
        )
    )

    assert "filesystem_name" in summary
    assert "bytes_available_percent" in summary
    assert "lustre-a" in summary


def test_summarize_lustre_metrics_with_filesystem_but_no_samples() -> None:
    """Mirrors the production state where the AMLFS is discovered but not yet mounted."""

    summary = summarize_lustre_metrics(
        ManagedLustreCollectionResult(metrics=(), filesystem_count=1)
    )

    assert "# no OST capacity samples returned" in summary
    assert "OSTBytesAvailable" not in summary


def test_summarize_lustre_metrics_with_no_filesystems() -> None:
    summary = summarize_lustre_metrics(
        ManagedLustreCollectionResult(metrics=(), filesystem_count=0)
    )

    assert "# no Azure Managed Lustre filesystems discovered" in summary


def test_normalize_ost_capacity_response_handles_unmounted_filesystem() -> None:
    """Azure Monitor returns metric entries with empty timeseries when the FS has no OSTs in use."""

    filesystem = ManagedLustreFilesystem(
        "sub-a",
        "rg-a",
        "lustre-a",
        "/subscriptions/sub-a/resourceGroups/rg-a/providers/"
        "Microsoft.StorageCache/amlFilesystems/lustre-a",
        "westus3",
    )

    metrics = normalize_ost_capacity_response(
        filesystem,
        {
            "metrics": [
                {"name": "OSTBytesAvailable", "timeseries": []},
                {"name": "OSTBytesUsed", "timeseries": []},
                {"name": "OSTBytesTotal", "timeseries": []},
            ]
        },
    )

    assert list(metrics) == []


def test_normalize_ost_capacity_response_rejects_scalar_metrics_field() -> None:
    """Defensive: never iterate a string as a sequence of characters."""

    filesystem = ManagedLustreFilesystem(
        "sub-a",
        "rg-a",
        "lustre-a",
        "/subscriptions/sub-a/resourceGroups/rg-a/providers/"
        "Microsoft.StorageCache/amlFilesystems/lustre-a",
        "westus3",
    )

    metrics = normalize_ost_capacity_response(filesystem, {"metrics": "OSTBytesAvailable"})

    assert list(metrics) == []
