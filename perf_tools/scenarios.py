# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License in the project root for
# license information.
# --------------------------------------------------------------------------
"""Shared operation definitions for throughput and memory benchmarks."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Callable
from unittest.mock import patch

from opentelemetry.sdk._logs import ReadableLogRecord
from opentelemetry.sdk._logs.export import LogRecordExporter, LogRecordExportResult
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

_FAKE_CONNECTION_STRING = (
    "InstrumentationKey=00000000-0000-0000-0000-000000000000;"
    "IngestionEndpoint=https://localhost/;"
    "LiveEndpoint=https://localhost/"
)
AZURE_MONITOR_LOGGER_NAME = "perf.azure_monitor_log"
OTEL_LOGGER_NAME = "perf.otel_log"


@dataclass(frozen=True)
class Scenario:
    case: str
    scenario: str
    category: str
    implementation: str
    gating: bool


@dataclass
class ManagedOperation:
    operation: Callable[[], None]
    shutdown: Callable[[], None]


SCENARIOS = {
    "azure_monitor_span": Scenario(
        case="azure_monitor_span",
        scenario="span",
        category="span",
        implementation="azure_monitor",
        gating=True,
    ),
    "azure_monitor_log": Scenario(
        case="azure_monitor_log",
        scenario="logger_emit",
        category="log",
        implementation="azure_monitor",
        gating=True,
    ),
    "otel_span": Scenario(
        case="otel_span",
        scenario="span",
        category="span",
        implementation="opentelemetry",
        gating=False,
    ),
    "otel_log": Scenario(
        case="otel_log",
        scenario="logger_emit",
        category="log",
        implementation="opentelemetry",
        gating=False,
    ),
}
SCENARIO_ORDER = tuple(SCENARIOS)


class _DiscardingSpanExporter(SpanExporter):
    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


class _DiscardingLogExporter(LogRecordExporter):
    def export(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
        return LogRecordExportResult.SUCCESS

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 10000) -> bool:
        return True


def make_span_operation(tracer: Any) -> Callable[[], None]:
    def operation() -> None:
        with tracer.start_as_current_span("bench") as span:
            span.set_attribute("bench.attr", 1)

    return operation


def make_log_operation(logger: logging.Logger) -> Callable[[], None]:
    def operation() -> None:
        logger.info("bench message %s", 1)

    return operation


def _azure_monitor_configurations(**overrides: Any) -> dict[str, Any]:
    from microsoft.opentelemetry._azure_monitor._utils.configurations import _get_configurations

    options = {
        "connection_string": _FAKE_CONNECTION_STRING,
        "disable_metrics": True,
        "enable_live_metrics": False,
        "enable_performance_counters": False,
        "disable_offline_storage": True,
        "logger_name": AZURE_MONITOR_LOGGER_NAME,
    }
    options.update(overrides)
    return _get_configurations(**options)


def create_managed_operation(case: str) -> ManagedOperation:
    if case not in SCENARIOS:
        raise ValueError(f"Unknown benchmark case: {case}")

    if case == "azure_monitor_span":
        from microsoft.opentelemetry._azure_monitor._configure import _setup_tracing

        with patch(
            "microsoft.opentelemetry._azure_monitor._configure.AzureMonitorTraceExporter",
            return_value=_DiscardingSpanExporter(),
        ):
            tracer_provider = _setup_tracing(_azure_monitor_configurations(disable_logging=True))
        operation = make_span_operation(tracer_provider.get_tracer("perf.azure_monitor_span"))
        return ManagedOperation(operation, tracer_provider.shutdown)

    if case == "azure_monitor_log":
        from microsoft.opentelemetry._azure_monitor._configure import _setup_logging

        logger = logging.getLogger(AZURE_MONITOR_LOGGER_NAME)
        original_handlers = set(logger.handlers)
        with patch(
            "azure.monitor.opentelemetry.exporter.AzureMonitorLogExporter",
            return_value=_DiscardingLogExporter(),
        ):
            logger_provider = _setup_logging(_azure_monitor_configurations(disable_tracing=True))
        if logger_provider is None:
            raise RuntimeError("Azure Monitor logging provider could not be configured")
        added_handlers = [handler for handler in logger.handlers if handler not in original_handlers]
        logger.setLevel(logging.INFO)
        logger.propagate = False

        def shutdown_azure_log() -> None:
            for handler in added_handlers:
                logger.removeHandler(handler)
            logger_provider.shutdown()

        return ManagedOperation(make_log_operation(logger), shutdown_azure_log)

    if case == "otel_span":
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        tracer_provider = TracerProvider()
        tracer_provider.add_span_processor(BatchSpanProcessor(_DiscardingSpanExporter()))
        operation = make_span_operation(tracer_provider.get_tracer("perf.otel_span"))
        return ManagedOperation(operation, tracer_provider.shutdown)

    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

    logger_provider = LoggerProvider()
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(_DiscardingLogExporter()))
    handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
    logger = logging.getLogger(OTEL_LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)

    def shutdown_otel_log() -> None:
        logger.removeHandler(handler)
        logger_provider.shutdown()

    return ManagedOperation(make_log_operation(logger), shutdown_otel_log)
