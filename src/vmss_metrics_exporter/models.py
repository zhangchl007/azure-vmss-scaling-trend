"""Shared data models for Azure metric collection."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VmssCount:
    """Normalized VM Scale Set count data ready for metric exposition."""

    subscription_id: str
    resource_group: str
    vmss_name: str
    location: str
    orchestration_mode: str
    actual_instance_count: int
    capacity: int
    vm_size: str = "unknown"
    sku_tier: str = "unknown"

    @property
    def label_values(self) -> tuple[str, str, str, str, str]:
        """Return labels for `azure_vmss_instance_count` and `azure_vmss_capacity`."""

        return (
            self.subscription_id,
            self.resource_group,
            self.vmss_name,
            self.location,
            self.orchestration_mode,
        )

    @property
    def info_label_values(self) -> tuple[str, str, str, str, str, str, str]:
        """Return labels for the `azure_vmss_info` metadata metric."""

        return (
            self.subscription_id,
            self.resource_group,
            self.vmss_name,
            self.location,
            self.orchestration_mode,
            self.vm_size,
            self.sku_tier,
        )


@dataclass(frozen=True, slots=True)
class ManagedLustreFilesystem:
    """Discovered Azure Managed Lustre filesystem resource."""

    subscription_id: str
    resource_group: str
    filesystem_name: str
    resource_id: str
    location: str
    sku_tier: str = "unknown"
    storage_capacity_tib: float = 0.0


@dataclass(frozen=True, slots=True)
class ManagedLustreOstMetric:
    """Normalized Azure Managed Lustre OST metric sample ready for Prometheus."""

    subscription_id: str
    resource_group: str
    filesystem_name: str
    location: str
    ostnum: str
    bytes_available: float
    bytes_used: float | None = None
    bytes_total: float | None = None
    sample_timestamp_seconds: float | None = None

    @property
    def label_values(self) -> tuple[str, str, str, str, str]:
        """Return labels for `azure_managed_lustre_ost_bytes_available`."""

        return (
            self.subscription_id,
            self.resource_group,
            self.filesystem_name,
            self.location,
            self.ostnum,
        )

    @property
    def bytes_available_percent(self) -> float | None:
        """Return available capacity percentage when total bytes are known."""

        if self.bytes_total is None or self.bytes_total <= 0:
            return None
        return (self.bytes_available / self.bytes_total) * 100

    @property
    def bytes_used_percent(self) -> float | None:
        """Return used capacity percentage when used and total bytes are known."""

        if self.bytes_used is None or self.bytes_total is None or self.bytes_total <= 0:
            return None
        return (self.bytes_used / self.bytes_total) * 100


@dataclass(frozen=True, slots=True)
class ManagedLustreCollectionResult:
    """Result of one Azure Managed Lustre collection pass."""

    metrics: tuple[ManagedLustreOstMetric, ...]
    filesystem_count: int
    error_count: int = 0
