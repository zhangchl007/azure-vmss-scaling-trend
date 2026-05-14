"""Prometheus metric exposition and polling loop for VMSS counts."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import suppress

from prometheus_client import REGISTRY, Counter, Gauge
from prometheus_client.registry import CollectorRegistry

from .models import VmssCount

LOGGER = logging.getLogger(__name__)

VMSS_LABELS = (
    "subscription_id",
    "resource_group",
    "vmss_name",
    "location",
    "orchestration_mode",
)


class VmssMetricsExporter:
    """Poll Azure and update Prometheus gauges with cached VMSS counts."""

    def __init__(
        self,
        collect_counts: Callable[[], Sequence[VmssCount]],
        *,
        poll_interval_seconds: int = 300,
        registry: CollectorRegistry | None = None,
    ) -> None:
        self._collect_counts = collect_counts
        self._poll_interval_seconds = poll_interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._metric_lock = threading.Lock()
        self._active_labelsets: set[tuple[str, str, str, str, str]] = set()

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
        self.last_success_timestamp.set(time.time())
        self.collection_duration.set(time.monotonic() - start)
        self.vmss_total.set(len(counts))
        LOGGER.info("Collected metrics for %s VM Scale Sets", len(counts))
        return counts

    def start(self) -> None:
        """Start the background polling thread."""

        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._poll_forever,
            name="vmss-metrics-poller",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Stop the background polling thread."""

        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _poll_forever(self) -> None:
        while not self._stop_event.is_set():
            with suppress(Exception):
                self.collect_once()
            self._stop_event.wait(self._poll_interval_seconds)

    def _update_metrics(self, counts: Sequence[VmssCount]) -> None:
        new_labelsets = {count.label_values for count in counts}
        with self._metric_lock:
            for stale in self._active_labelsets - new_labelsets:
                self.instance_count.remove(*stale)
                self.capacity.remove(*stale)

            for count in counts:
                labels = count.label_values
                self.instance_count.labels(*labels).set(count.actual_instance_count)
                self.capacity.labels(*labels).set(count.capacity)

            self._active_labelsets = new_labelsets
