"""Shared data models for VMSS metric collection."""

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

    @property
    def label_values(self) -> tuple[str, str, str, str, str]:
        """Return labels in the same order used by Prometheus gauges."""

        return (
            self.subscription_id,
            self.resource_group,
            self.vmss_name,
            self.location,
            self.orchestration_mode,
        )
