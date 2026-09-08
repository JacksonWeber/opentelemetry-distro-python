# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License in the project root for
# license information.
# --------------------------------------------------------------------------
"""Tests for the local OTLP performance exporter."""

# pylint: disable=redefined-outer-name

from __future__ import annotations

import importlib.metadata
import json
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import requests

from perf_tools import export
from perf_tools.scenarios import SCENARIO_ORDER, SCENARIOS


def _benchmark_document() -> dict[str, Any]:
    return {
        "benchmarks": [
            {
                "name": f"test_{case}",
                "stats": {
                    "median": 0.001 * (index + 1),
                    "iterations": 10 + index,
                    "rounds": 5,
                },
                "extra_info": {"gating": SCENARIOS[case].gating},
            }
            for index, case in enumerate(SCENARIO_ORDER)
        ]
    }


def _raw_trials() -> dict[str, list[dict[str, int]]]:
    return {
        case: [
            {
                "heap_used_increase": -100,
                "retained_heap_increase": 1000,
                "rss_increase": 0,
                "rss_retained_increase": 0,
            },
            {
                "heap_used_increase": 8192,
                "retained_heap_increase": 12288,
                "rss_increase": 16384,
                "rss_retained_increase": 8192,
            },
            {
                "heap_used_increase": 12288,
                "retained_heap_increase": 16384,
                "rss_increase": 24576,
                "rss_retained_increase": 12288,
            },
        ]
        for case in SCENARIO_ORDER
    }


def _decode_attributes(attributes: list[dict[str, Any]]) -> dict[str, Any]:
    decoded = {}
    for attribute in attributes:
        value = attribute["value"]
        if "stringValue" in value:
            decoded[attribute["key"]] = value["stringValue"]
        elif "boolValue" in value:
            decoded[attribute["key"]] = value["boolValue"]
        elif "intValue" in value:
            decoded[attribute["key"]] = int(value["intValue"])
        else:
            decoded[attribute["key"]] = value["doubleValue"]
    return decoded


def _build_complete_payload() -> tuple[dict[str, Any], dict[str, Any]]:
    identity = export.collect_identity(require_export_identity=True)
    throughput = export.parse_benchmark_results(_benchmark_document(), SCENARIO_ORDER)
    _, memory = export.aggregate_memory(_raw_trials(), iterations=1000, trials=3, noise_floor_bytes=4096)
    resources = export.resource_attributes("run-test-123", identity)
    return export.build_otlp_payload([*throughput, *memory], resources, timestamp_ns=123), identity


def test_scenario_mapping_preserves_shared_and_python_identities():
    mapping = export.test_scenario_mapping()

    assert [item["case"] for item in mapping] == list(SCENARIO_ORDER)
    assert {item["test"] for item in mapping} == set(SCENARIO_ORDER)
    assert all(item["test"] == item["case"] for item in mapping)
    assert all(item["name"] == item["scenario"] for item in mapping)
    assert {(item["scenario"], item["category"]) for item in mapping} == {
        ("span", "span"),
        ("logger_emit", "log"),
    }
    assert {item["implementation"] for item in mapping} == {"azure_monitor", "opentelemetry"}


def test_otlp_payload_has_exact_metric_contract_and_dimensions():
    payload, identity = _build_complete_payload()
    resource_metric = payload["resourceMetrics"][0]
    resources = _decode_attributes(resource_metric["resource"]["attributes"])
    metrics = resource_metric["scopeMetrics"][0]["metrics"]
    expected_units = {item["name"]: item["unit"] for item in export.METRIC_CONTRACT}

    assert resources == {
        "service.name": "microsoft-opentelemetry-python-benchmark",
        "benchmark.run_id": "run-test-123",
        "benchmark.source": "opentelemetry-distro-python",
        "package.name": "microsoft-opentelemetry",
        "package.version": importlib.metadata.version("microsoft-opentelemetry"),
        "vcs.revision": identity["commit"],
        "process.runtime.name": "python",
        "process.runtime.version": identity["runtimeVersion"],
        "os.type": identity["osType"],
        "os.version": identity["osVersion"],
        "host.arch": identity["hostArch"],
    }
    assert len(identity["commit"]) == 40
    assert {metric["name"]: metric["unit"] for metric in metrics} == expected_units
    assert all(set(metric) >= {"name", "unit", "gauge"} for metric in metrics)

    points = [(metric["name"], point) for metric in metrics for point in metric["gauge"]["dataPoints"]]
    assert len(points) == 24
    assert {name for name, _ in points} == set(expected_units)

    cases = set()
    tests = set()
    scenario_implementations = set()
    for metric_name, point in points:
        attributes = _decode_attributes(point["attributes"])
        cases.add(attributes["benchmark.case"])
        tests.add(attributes["benchmark.test"])
        scenario_implementations.add((attributes["benchmark.scenario"], attributes["benchmark.implementation"]))
        assert attributes["benchmark.test"] == attributes["benchmark.case"]
        assert attributes["benchmark.name"] == attributes["benchmark.scenario"]
        assert attributes["benchmark.category"] in {"span", "log"}
        if metric_name.endswith("throughput"):
            assert attributes["benchmark.statistic"] == "median"
            assert attributes["benchmark.iterations"] > 0
            assert attributes["benchmark.rounds"] == 5
            assert isinstance(attributes["benchmark.gating"], bool)
            assert attributes["benchmark.unit"] == "operations/s"
        else:
            assert attributes["benchmark.statistic"] == "median_observed_increase"
            assert attributes["benchmark.memory_iterations"] == 1000
            assert attributes["benchmark.memory_trials"] == 3
            assert attributes["benchmark.memory_noise_floor_bytes"] == 4096
            assert point["asDouble"] >= 0

    assert cases == set(SCENARIO_ORDER)
    assert tests == set(SCENARIO_ORDER)
    assert scenario_implementations == {
        ("span", "azure_monitor"),
        ("span", "opentelemetry"),
        ("logger_emit", "azure_monitor"),
        ("logger_emit", "opentelemetry"),
    }


def test_memory_aggregation_keeps_signed_trials_local_and_clamps_noise():
    aggregates, points = export.aggregate_memory(
        _raw_trials(),
        iterations=1000,
        trials=3,
        noise_floor_bytes=4096,
    )

    assert all(aggregate["heap_used_increase_signed_median"] == 8192 for aggregate in aggregates.values())
    assert all(aggregate["retained_heap_increase_signed_median"] == 12288 for aggregate in aggregates.values())
    assert all(aggregate["rss_increase_signed_median"] == 16384 for aggregate in aggregates.values())
    assert all(float(point["value"]) >= 0 for point in points)

    noisy_trials = {
        "otel_span": [
            {"heap_used_increase": -1, "retained_heap_increase": 1, "rss_increase": 4096},
            {"heap_used_increase": 0, "retained_heap_increase": 4096, "rss_increase": 4096},
            {"heap_used_increase": 1, "retained_heap_increase": 4097, "rss_increase": 5000},
        ]
    }
    noisy_aggregates, noisy_points = export.aggregate_memory(
        noisy_trials,
        iterations=10,
        trials=3,
        noise_floor_bytes=4096,
    )
    assert noisy_aggregates["otel_span"]["heap_used_increase"] == 0
    assert noisy_aggregates["otel_span"]["retained_heap_increase"] == 0
    assert noisy_aggregates["otel_span"]["rss_increase"] == 0
    assert all(point["value"] == 0 for point in noisy_points)


@pytest.mark.parametrize(
    ("cli_endpoint", "environment", "expected"),
    [
        ("https://collector.example/otlp", {}, "https://collector.example/otlp/v1/metrics"),
        (
            "https://collector.example/otlp/v1/metrics/",
            {},
            "https://collector.example/otlp/v1/metrics",
        ),
        (
            None,
            {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://env.example/otlp"},
            "https://env.example/otlp/v1/metrics",
        ),
        (
            "https://collector.example/otlp?api-version=1",
            {},
            "https://collector.example/otlp/v1/metrics?api-version=1",
        ),
        (
            "https://collector.example/otlp/v1/metrics?api-version=1",
            {},
            "https://collector.example/otlp/v1/metrics?api-version=1",
        ),
    ],
)
def test_resolve_otlp_metrics_endpoint(cli_endpoint, environment, expected):
    assert export.resolve_otlp_metrics_endpoint(cli_endpoint, environment) == expected


def test_resolve_otlp_metrics_endpoint_requires_configuration():
    with pytest.raises(ValueError, match="OTEL_EXPORTER_OTLP_ENDPOINT"):
        export.resolve_otlp_metrics_endpoint(None, {})


def test_redact_endpoint_hides_userinfo_query_values_and_fragments():
    endpoint = "https://user:password@collector.example:443/otlp?sig=secret&api-version=1#token"

    assert (
        export.redact_endpoint(endpoint)
        == "https://***@collector.example:443/otlp?sig=REDACTED&api-version=REDACTED#REDACTED"
    )
    assert export.redact_endpoint(None) is None


class _CollectorHandler(BaseHTTPRequestHandler):
    status_code = 202
    request_path = ""
    request_headers: dict[str, str] = {}
    request_body = b""

    def do_POST(self):  # pylint: disable=invalid-name
        type(self).request_path = self.path
        type(self).request_headers = dict(self.headers)
        type(self).request_body = self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(type(self).status_code)
        self.end_headers()
        if type(self).status_code >= 400:
            self.wfile.write(b"collector rejected payload")

    def log_message(self, format, *args):  # pylint: disable=redefined-builtin
        return


@pytest.fixture
def collector():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CollectorHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_post_otlp_payload_uses_json_and_propagates_non_success(collector):
    payload, _ = _build_complete_payload()
    _CollectorHandler.status_code = 202

    export.post_otlp_payload(f"{collector}/v1/metrics", payload, timeout=5)

    assert _CollectorHandler.request_path == "/v1/metrics"
    assert _CollectorHandler.request_headers["Content-Type"] == "application/json"
    assert json.loads(_CollectorHandler.request_body) == payload

    _CollectorHandler.status_code = 503
    with pytest.raises(RuntimeError, match="HTTP 503"):
        export.post_otlp_payload(f"{collector}/v1/metrics", payload, timeout=5)


def test_post_otlp_payload_propagates_connection_errors():
    payload, _ = _build_complete_payload()
    with socket.socket() as closed_socket:
        closed_socket.bind(("127.0.0.1", 0))
        port = closed_socket.getsockname()[1]

    with pytest.raises(RuntimeError, match="OTLP export request failed"):
        export.post_otlp_payload(f"http://127.0.0.1:{port}/v1/metrics", payload, timeout=0.2)


def test_post_otlp_payload_redacts_credentials_from_transport_errors(monkeypatch):
    endpoint = "https://user:password@collector.example/otlp/v1/metrics?sig=TOPSECRET"

    def fail_request(*args, **kwargs):
        raise requests.ConnectionError(f"could not connect to {endpoint}")

    monkeypatch.setattr(requests, "post", fail_request)

    with pytest.raises(RuntimeError) as error:
        export.post_otlp_payload(endpoint, {"resourceMetrics": []}, timeout=1)
    assert "TOPSECRET" not in str(error.value)
    assert "password" not in str(error.value)
    assert "sig=REDACTED" in str(error.value)


def test_collect_identity_uses_real_package_version_and_checkout_commit():
    identity = export.collect_identity(require_export_identity=True)
    expected_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert identity["packageName"] == "microsoft-opentelemetry"
    assert identity["packageVersion"] == importlib.metadata.version("microsoft-opentelemetry")
    assert identity["commit"] == expected_commit


def test_collect_identity_requires_revision_only_for_export(monkeypatch):
    real_run_git = export._run_git

    def run_git_without_revision(arguments, cwd):
        if arguments == ("rev-parse", "HEAD"):
            return None
        return real_run_git(arguments, cwd)

    monkeypatch.setattr(export, "_run_git", run_git_without_revision)

    assert export.collect_identity(require_export_identity=False)["commit"] is None
    with pytest.raises(RuntimeError, match="vcs.revision"):
        export.collect_identity(require_export_identity=True)


def test_parse_benchmark_results_requires_every_case():
    with pytest.raises(ValueError, match="Missing pytest-benchmark results"):
        export.parse_benchmark_results({"benchmarks": []}, SCENARIO_ORDER)
