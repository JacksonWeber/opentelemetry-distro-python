# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License in the project root for
# license information.
# --------------------------------------------------------------------------
"""Performance benchmarks for the Microsoft OpenTelemetry distro.

These tests are skipped by default — they only execute when pytest is invoked
with ``--benchmark-only`` (or any other ``--benchmark-*`` flag) by the
performance CI workflow.

Two pairs of scenarios are measured:

* ``azure_monitor_span`` / ``azure_monitor_log`` (gating): the per-operation
  cost the distro adds on top of upstream OpenTelemetry.
* ``otel_span`` / ``otel_log`` (informational): a plain upstream
  ``opentelemetry-sdk`` reference. They are reported but never fail the
  build.
"""

# pylint: disable=redefined-outer-name
# (pytest fixtures shadow the fixture function name by design.)

from __future__ import annotations

import pytest

from perf_tools.scenarios import SCENARIOS, create_managed_operation


@pytest.fixture
def azure_monitor_span_operation():
    managed = create_managed_operation("azure_monitor_span")
    try:
        yield managed.operation
    finally:
        managed.shutdown()


@pytest.fixture
def azure_monitor_log_operation():
    managed = create_managed_operation("azure_monitor_log")
    try:
        yield managed.operation
    finally:
        managed.shutdown()


@pytest.fixture
def otel_span_operation():
    managed = create_managed_operation("otel_span")
    try:
        yield managed.operation
    finally:
        managed.shutdown()


@pytest.fixture
def otel_log_operation():
    managed = create_managed_operation("otel_log")
    try:
        yield managed.operation
    finally:
        managed.shutdown()


def _mark_gating(benchmark, case: str) -> None:
    benchmark.extra_info["gating"] = SCENARIOS[case].gating


@pytest.mark.benchmark(group="span")
def test_azure_monitor_span(benchmark, azure_monitor_span_operation):
    _mark_gating(benchmark, "azure_monitor_span")
    benchmark(azure_monitor_span_operation)


@pytest.mark.benchmark(group="log")
def test_azure_monitor_log(benchmark, azure_monitor_log_operation):
    _mark_gating(benchmark, "azure_monitor_log")
    benchmark(azure_monitor_log_operation)


@pytest.mark.benchmark(group="span")
def test_otel_span(benchmark, otel_span_operation):
    _mark_gating(benchmark, "otel_span")
    benchmark(otel_span_operation)


@pytest.mark.benchmark(group="log")
def test_otel_log(benchmark, otel_log_operation):
    _mark_gating(benchmark, "otel_log")
    benchmark(otel_log_operation)
