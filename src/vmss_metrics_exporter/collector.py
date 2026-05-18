"""Prometheus metric exposition and polling loop for VMSS counts."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import suppress

from prometheus_client import REGISTRY, Counter, Gauge
from prometheus_client.registry import CollectorRegistry

from .models import ManagedLustreCollectionResult, ManagedLustreOstMetric, VmssCount

LOGGER = logging.getLogger(__name__)

VMSS_LABELS = (
    "subscription_id",
    "resource_group",
    "vmss_name",
    "location",
    "orchestration_mode",
)

VMSS_INFO_LABELS = (
    "subscription_id",
    "resource_group",
    "vmss_name",
    "location",
    "orchestration_mode",
    "vm_size",
    "sku_tier",
)

LUSTRE_OST_LABELS = (
    "subscription_id",
    "resource_group",
    "filesystem_name",
    "location",
    "ostnum",
)


class VmssMetricsExporter:
    """Poll Azure and update Prometheus gauges with cached VMSS counts."""

    def __init__(
        self,
        collect_counts: Callable[[], Sequence[VmssCount]],
        *,
        collect_lustre_metrics: Callable[[], ManagedLustreCollectionResult] | None = None,
        poll_interval_seconds: int = 300,
        lustre_poll_interval_seconds: int = 60,
        registry: CollectorRegistry | None = None,
    ) -> None:
        self._collect_counts = collect_counts
        self._collect_lustre_metrics = collect_lustre_metrics
        self._poll_interval_seconds = poll_interval_seconds
        self._lustre_poll_interval_seconds = lustre_poll_interval_seconds
        self._stop_event = threading.Event()
        self._vmss_thread: threading.Thread | None = None
        self._lustre_thread: threading.Thread | None = None
        self._metric_lock = threading.Lock()
        self._active_labelsets: set[tuple[str, str, str, str, str]] = set()
        self._active_info_labelsets: set[tuple[str, str, str, str, str, str, str]] = set()
        self._active_lustre_ost_labelsets: set[tuple[str, str, str, str, str]] = set()

        effective_registry = registry if registry is not None else REGISTRY

        self.instance_count = Gauge(
            "azure_vmss_instance_count",
            "Actual Azure VM Scale Set virtual machine instance count from Azure Resource Graph.",
            VMSS_LABELS,
            registry=effective_registry,
        )
        self.capacity = Gauge(
            "azure_vmss_capacity",
            "Desired Azure VM Scale Set capacity from the parent VMSS resource sku.capacity.",
            VMSS_LABELS,
            registry=effective_registry,
        )
        self.info = Gauge(
            "azure_vmss_info",
            (
                "Static metadata about each VM Scale Set (vm_size from sku.name, "
                "sku_tier from sku.tier). The value is always 1; join via "
                "`* on (subscription_id, resource_group, vmss_name) group_left(vm_size, sku_tier)`."
            ),
            VMSS_INFO_LABELS,
            registry=effective_registry,
        )
        self.last_success_timestamp = Gauge(
            "azure_vmss_exporter_last_success_timestamp_seconds",
            "Unix timestamp of the last successful Azure Resource Graph collection.",
            registry=effective_registry,
        )
        self.collection_duration = Gauge(
            "azure_vmss_exporter_collection_duration_seconds",
            "Duration in seconds of the most recent Azure Resource Graph collection attempt.",
            registry=effective_registry,
        )
        self.collection_errors = Counter(
            "azure_vmss_exporter_collection_errors",
            "Total Azure Resource Graph collection errors observed by this exporter process.",
            registry=effective_registry,
        )
        self.vmss_total = Gauge(
            "azure_vmss_exporter_vmss_total",
            "Number of Azure VM Scale Sets observed in the latest successful collection.",
            registry=effective_registry,
        )
        self.lustre_ost_bytes_available = Gauge(
            "azure_managed_lustre_ost_bytes_available",
            "Azure Managed Lustre OST bytes available from Azure Monitor OSTBytesAvailable.",
            LUSTRE_OST_LABELS,
            registry=effective_registry,
        )
        self.lustre_ost_bytes_used = Gauge(
            "azure_managed_lustre_ost_bytes_used",
            "Azure Managed Lustre OST bytes used from Azure Monitor OSTBytesUsed.",
            LUSTRE_OST_LABELS,
            registry=effective_registry,
        )
        self.lustre_ost_bytes_total = Gauge(
            "azure_managed_lustre_ost_bytes_total",
            "Azure Managed Lustre OST total bytes from Azure Monitor OSTBytesTotal.",
            LUSTRE_OST_LABELS,
            registry=effective_registry,
        )
        self.lustre_ost_bytes_available_percent = Gauge(
            "azure_managed_lustre_ost_bytes_available_percent",
            (
                "Azure Managed Lustre OST available capacity percentage derived from "
                "bytes available / total."
            ),
            LUSTRE_OST_LABELS,
            registry=effective_registry,
        )
        self.lustre_ost_bytes_used_percent = Gauge(
            "azure_managed_lustre_ost_bytes_used_percent",
            "Azure Managed Lustre OST used capacity percentage derived from bytes used / total.",
            LUSTRE_OST_LABELS,
            registry=effective_registry,
        )
        self.lustre_ost_sample_timestamp = Gauge(
            "azure_managed_lustre_ost_sample_timestamp_seconds",
            "Unix timestamp of the Azure Monitor sample backing each OSTBytesAvailable series.",
            LUSTRE_OST_LABELS,
            registry=effective_registry,
        )
        self.lustre_filesystem_total = Gauge(
            "azure_managed_lustre_filesystem_total",
            "Number of Azure Managed Lustre filesystems discovered in the latest collection.",
            registry=effective_registry,
        )
        self.lustre_ost_total = Gauge(
            "azure_managed_lustre_ost_total",
            "Number of Azure Managed Lustre OST series observed in the latest collection.",
            registry=effective_registry,
        )
        self.lustre_last_success_timestamp = Gauge(
            "azure_managed_lustre_last_success_timestamp_seconds",
            "Unix timestamp of the last successful Azure Managed Lustre collection.",
            registry=effective_registry,
        )
        self.lustre_collection_duration = Gauge(
            "azure_managed_lustre_collection_duration_seconds",
            "Duration in seconds of the most recent Azure Managed Lustre collection attempt.",
            registry=effective_registry,
        )
        self.lustre_collection_errors = Counter(
            "azure_managed_lustre_collection_errors",
            "Total Azure Managed Lustre collection errors observed by this exporter process.",
            registry=effective_registry,
        )

    def collect_once(self) -> Sequence[VmssCount]:
        """Collect once and immediately update gauges."""

        start = time.monotonic()
        try:
            counts = tuple(self._collect_counts())
        except Exception:  # noqa: BLE001 - keep exporter alive for transient Azure failures.
            self.collection_errors.inc()
            self.collection_duration.set(time.monotonic() - start)
            LOGGER.exception("VMSS metric collection failed")
            raise

        self._update_metrics(counts)
        if self._collect_lustre_metrics is not None:
            self.collect_lustre_once()
        self.last_success_timestamp.set(time.time())
        self.collection_duration.set(time.monotonic() - start)
        self.vmss_total.set(len(counts))
        LOGGER.info("Collected metrics for %s VM Scale Sets", len(counts))
        return counts

    def collect_lustre_once(self) -> ManagedLustreCollectionResult | None:
        """Collect Azure Managed Lustre once and immediately update gauges."""

        if self._collect_lustre_metrics is None:
            return None
        start = time.monotonic()
        try:
            result = self._collect_lustre_metrics()
        except Exception:  # noqa: BLE001 - keep VMSS metrics alive if Azure Monitor fails.
            self.lustre_collection_errors.inc()
            self.lustre_collection_duration.set(time.monotonic() - start)
            LOGGER.exception("Azure Managed Lustre metric collection failed")
            raise

        remove_stale = result.error_count == 0
        self._update_lustre_metrics(result.metrics, remove_stale=remove_stale)
        self.lustre_filesystem_total.set(result.filesystem_count)
        self.lustre_ost_total.set(len(result.metrics))
        if result.error_count:
            self.lustre_collection_errors.inc(result.error_count)
        self.lustre_last_success_timestamp.set(time.time())
        self.lustre_collection_duration.set(time.monotonic() - start)
        LOGGER.info(
            "Collected metrics for %s Managed Lustre filesystems and %s OST series%s",
            result.filesystem_count,
            len(result.metrics),
            " with partial errors" if result.error_count else "",
        )
        return result

    def start(self) -> None:
        """Start the background polling thread."""

        if self._vmss_thread and self._vmss_thread.is_alive():
            return
        self._vmss_thread = threading.Thread(
            target=self._poll_vmss_forever,
            name="vmss-metrics-poller",
            daemon=True,
        )
        self._vmss_thread.start()
        if self._collect_lustre_metrics is not None:
            self._lustre_thread = threading.Thread(
                target=self._poll_lustre_forever,
                name="managed-lustre-metrics-poller",
                daemon=True,
            )
            self._lustre_thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Stop the background polling thread."""

        self._stop_event.set()
        if self._vmss_thread:
            self._vmss_thread.join(timeout=timeout)
        if self._lustre_thread:
            self._lustre_thread.join(timeout=timeout)

    def _poll_vmss_forever(self) -> None:
        while not self._stop_event.is_set():
            with suppress(Exception):
                self._collect_vmss_once()
            self._stop_event.wait(self._poll_interval_seconds)

    def _poll_lustre_forever(self) -> None:
        while not self._stop_event.is_set():
            with suppress(Exception):
                self.collect_lustre_once()
            self._stop_event.wait(self._lustre_poll_interval_seconds)

    def _collect_vmss_once(self) -> Sequence[VmssCount]:
        start = time.monotonic()
        try:
            counts = tuple(self._collect_counts())
        except Exception:  # noqa: BLE001 - keep exporter alive for transient Azure failures.
            self.collection_errors.inc()
            self.collection_duration.set(time.monotonic() - start)
            LOGGER.exception("VMSS metric collection failed")
            raise

        self._update_metrics(counts)
        self.last_success_timestamp.set(time.time())
        self.collection_duration.set(time.monotonic() - start)
        self.vmss_total.set(len(counts))
        LOGGER.info("Collected metrics for %s VM Scale Sets", len(counts))
        return counts

    def _update_metrics(self, counts: Sequence[VmssCount]) -> None:
        new_labelsets = {count.label_values for count in counts}
        new_info_labelsets = {count.info_label_values for count in counts}
        with self._metric_lock:
            for stale in self._active_labelsets - new_labelsets:
                self.instance_count.remove(*stale)
                self.capacity.remove(*stale)
            for stale_info in self._active_info_labelsets - new_info_labelsets:
                self.info.remove(*stale_info)

            for count in counts:
                labels = count.label_values
                self.instance_count.labels(*labels).set(count.actual_instance_count)
                self.capacity.labels(*labels).set(count.capacity)
                self.info.labels(*count.info_label_values).set(1)

            self._active_labelsets = new_labelsets
            self._active_info_labelsets = new_info_labelsets

    def _update_lustre_metrics(
        self,
        metrics: Sequence[ManagedLustreOstMetric],
        *,
        remove_stale: bool,
    ) -> None:
        new_labelsets = {metric.label_values for metric in metrics}
        with self._metric_lock:
            if remove_stale:
                for stale in self._active_lustre_ost_labelsets - new_labelsets:
                    self.lustre_ost_bytes_available.remove(*stale)
                    self.lustre_ost_bytes_used.remove(*stale)
                    self.lustre_ost_bytes_total.remove(*stale)
                    self.lustre_ost_bytes_available_percent.remove(*stale)
                    self.lustre_ost_bytes_used_percent.remove(*stale)
                    self.lustre_ost_sample_timestamp.remove(*stale)

            for metric in metrics:
                self.lustre_ost_bytes_available.labels(*metric.label_values).set(
                    metric.bytes_available
                )
                if metric.bytes_used is not None:
                    self.lustre_ost_bytes_used.labels(*metric.label_values).set(metric.bytes_used)
                else:
                    with suppress(KeyError):
                        self.lustre_ost_bytes_used.remove(*metric.label_values)
                if metric.bytes_total is not None:
                    self.lustre_ost_bytes_total.labels(*metric.label_values).set(metric.bytes_total)
                else:
                    with suppress(KeyError):
                        self.lustre_ost_bytes_total.remove(*metric.label_values)
                if metric.bytes_available_percent is not None:
                    self.lustre_ost_bytes_available_percent.labels(*metric.label_values).set(
                        metric.bytes_available_percent
                    )
                else:
                    with suppress(KeyError):
                        self.lustre_ost_bytes_available_percent.remove(*metric.label_values)
                if metric.bytes_used_percent is not None:
                    self.lustre_ost_bytes_used_percent.labels(*metric.label_values).set(
                        metric.bytes_used_percent
                    )
                else:
                    with suppress(KeyError):
                        self.lustre_ost_bytes_used_percent.remove(*metric.label_values)
                self.lustre_ost_sample_timestamp.labels(*metric.label_values).set(
                    metric.sample_timestamp_seconds or time.time()
                )

            if remove_stale:
                self._active_lustre_ost_labelsets = new_labelsets
            else:
                self._active_lustre_ost_labelsets |= new_labelsets
