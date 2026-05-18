"""Command-line entry point for the VMSS metrics exporter."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from prometheus_client import start_http_server

from .azure_managed_lustre import (
    AzureManagedLustreCollector,
    create_metrics_query_client,
    summarize_lustre_metrics,
)
from .azure_resource_graph import (
    AzureResourceGraphVmssCollector,
    ResourceGraphClientProtocol,
    create_resource_graph_client,
    summarize_counts,
)
from .collector import VmssMetricsExporter
from .config import Settings, load_settings

LOGGER = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    """Run the exporter or perform a one-shot collection."""

    parser = argparse.ArgumentParser(description="Export Azure VMSS instance counts to Prometheus.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Collect once, print a tab-separated summary, then exit without starting /metrics.",
    )
    args = parser.parse_args(argv)

    try:
        settings = load_settings(require_subscription_ids=True)
        _configure_logging(settings.log_level)
        resource_graph_client = create_resource_graph_client()
        collector = AzureResourceGraphVmssCollector(
            resource_graph_client,
            settings.subscription_ids,
            page_size=settings.arg_page_size,
            max_retries=settings.arg_max_retries,
            retry_base_delay_seconds=settings.arg_retry_base_delay_seconds,
        )
        lustre_collector = _create_lustre_collector(settings, resource_graph_client)

        if args.once:
            print(summarize_counts(collector.collect()))
            if lustre_collector is not None:
                print()
                print("Azure Managed Lustre OSTBytesAvailable")
                print(summarize_lustre_metrics(lustre_collector.collect()))
            return 0

        exporter = VmssMetricsExporter(
            collector.collect,
            collect_lustre_metrics=lustre_collector.collect if lustre_collector else None,
            poll_interval_seconds=settings.poll_interval_seconds,
            lustre_poll_interval_seconds=settings.lustre_poll_interval_seconds,
        )
        start_http_server(settings.port, addr=settings.host)
        LOGGER.info(
            "VMSS metrics exporter listening on http://%s:%s/metrics; VMSS polling every %ss; "
            "Managed Lustre metrics %s%s",
            settings.host,
            settings.port,
            settings.poll_interval_seconds,
            "enabled" if lustre_collector else "disabled",
            f" every {settings.lustre_poll_interval_seconds}s" if lustre_collector else "",
        )
        exporter.start()
        _wait_for_shutdown_signal()
        exporter.stop()
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI should present actionable errors.
        logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s: %(message)s")
        LOGGER.exception("Exporter failed to start: %s", exc)
        return 1


def _create_lustre_collector(
    settings: Settings,
    resource_graph_client: ResourceGraphClientProtocol,
) -> AzureManagedLustreCollector | None:
    if not settings.enable_managed_lustre_metrics:
        return None
    return AzureManagedLustreCollector(
        resource_graph_client,
        create_metrics_query_client(),
        settings.subscription_ids,
        page_size=settings.arg_page_size,
        lookback_minutes=settings.lustre_metrics_lookback_minutes,
        interval=settings.lustre_metrics_interval,
        max_workers=settings.lustre_metrics_max_workers,
        max_retries=settings.arg_max_retries,
        retry_base_delay_seconds=settings.arg_retry_base_delay_seconds,
    )


def _configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if level > logging.DEBUG:
        logging.getLogger("azure").setLevel(logging.WARNING)


def _wait_for_shutdown_signal() -> None:
    stop_event = threading.Event()

    def _request_stop(signum: int, _frame: object) -> None:
        LOGGER.info("Received signal %s; shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    stop_event.wait()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
