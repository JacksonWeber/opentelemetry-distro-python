# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License in the project root for
# license information.
# --------------------------------------------------------------------------
"""Run local performance tests and export completed results as OTLP/HTTP JSON."""

from __future__ import annotations

import argparse
import gc
import importlib.metadata
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psutil
import requests

from perf_tools.scenarios import SCENARIOS, SCENARIO_ORDER, create_managed_operation

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DISTRIBUTION = "microsoft-opentelemetry"
SERVICE_NAME = "microsoft-opentelemetry-python-benchmark"
BENCHMARK_SOURCE = "opentelemetry-distro-python"
SCHEMA_VERSION = "1.0.0"

METRIC_CONTRACT = (
    {
        "name": "microsoft.opentelemetry.benchmark.throughput",
        "unit": "{operation}/s",
        "statistic": "median",
    },
    {
        "name": "microsoft.opentelemetry.benchmark.memory.heap_used_increase",
        "unit": "By",
        "statistic": "median_observed_increase",
    },
    {
        "name": "microsoft.opentelemetry.benchmark.memory.heap_used_increase_per_operation",
        "unit": "By/{operation}",
        "statistic": "median_observed_increase",
    },
    {
        "name": "microsoft.opentelemetry.benchmark.memory.retained_heap_increase",
        "unit": "By",
        "statistic": "median_observed_increase",
    },
    {
        "name": "microsoft.opentelemetry.benchmark.memory.retained_heap_increase_per_operation",
        "unit": "By/{operation}",
        "statistic": "median_observed_increase",
    },
    {
        "name": "microsoft.opentelemetry.benchmark.memory.rss_increase",
        "unit": "By",
        "statistic": "median_observed_increase",
    },
)

_MEMORY_METRICS = (
    ("heap_used_increase", "microsoft.opentelemetry.benchmark.memory.heap_used_increase", "By"),
    (
        "heap_used_increase_per_operation",
        "microsoft.opentelemetry.benchmark.memory.heap_used_increase_per_operation",
        "By/{operation}",
    ),
    ("retained_heap_increase", "microsoft.opentelemetry.benchmark.memory.retained_heap_increase", "By"),
    (
        "retained_heap_increase_per_operation",
        "microsoft.opentelemetry.benchmark.memory.retained_heap_increase_per_operation",
        "By/{operation}",
    ),
    ("rss_increase", "microsoft.opentelemetry.benchmark.memory.rss_increase", "By"),
)


def test_scenario_mapping() -> list[dict[str, Any]]:
    return [
        {
            "case": scenario.case,
            "test": scenario.case,
            "scenario": scenario.scenario,
            "name": scenario.scenario,
            "category": scenario.category,
            "implementation": scenario.implementation,
            "gating": scenario.gating,
        }
        for scenario in SCENARIOS.values()
    ]


def resolve_otlp_metrics_endpoint(
    cli_endpoint: str | None,
    environ: Mapping[str, str] | None = None,
) -> str:
    environment = os.environ if environ is None else environ
    endpoint = cli_endpoint or environment.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        raise ValueError("Set --otlp-endpoint or OTEL_EXPORTER_OTLP_ENDPOINT, or pass --no-export")
    parsed = urlsplit(endpoint)
    path = parsed.path.rstrip("/")
    if not path.endswith("/v1/metrics"):
        path = f"{path}/v1/metrics"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


def redact_endpoint(endpoint: str | None) -> str | None:
    if endpoint is None:
        return None
    parsed = urlsplit(endpoint)
    hostname = parsed.hostname or ""
    if ":" in hostname:
        hostname = f"[{hostname}]"
    host = f"{hostname}:{parsed.port}" if parsed.port else hostname
    netloc = f"***@{host}" if parsed.username is not None else host
    query = urlencode([(key, "REDACTED") for key, _ in parse_qsl(parsed.query, keep_blank_values=True)])
    fragment = "REDACTED" if parsed.fragment else ""
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, fragment))


def _run_git(arguments: Sequence[str], cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


def collect_identity(require_export_identity: bool) -> dict[str, Any]:
    import microsoft.opentelemetry

    distribution = importlib.metadata.distribution(PACKAGE_DISTRIBUTION)
    package_name = str(distribution.metadata["Name"] or "").strip()
    package_version = distribution.version.strip()
    imported_version = str(microsoft.opentelemetry.__version__).strip()
    package_file = Path(microsoft.opentelemetry.__file__).resolve()
    checkout_root_text = _run_git(("rev-parse", "--show-toplevel"), package_file.parent)
    checkout_root = Path(checkout_root_text) if checkout_root_text else None
    revision = _run_git(("rev-parse", "HEAD"), checkout_root) if checkout_root else None

    errors: list[str] = []
    if not package_name:
        errors.append("package.name is empty")
    if not package_version:
        errors.append("package.version is empty")
    if imported_version != package_version:
        errors.append(
            f"installed distribution version {package_version!r} does not match "
            f"imported distro version {imported_version!r}"
        )
    if require_export_identity and not revision:
        errors.append("vcs.revision cannot be resolved from the imported package checkout")
    if errors:
        raise RuntimeError("; ".join(errors))

    branch = _run_git(("branch", "--show-current"), checkout_root) if checkout_root else None
    return {
        "packageName": package_name,
        "packageVersion": package_version,
        "packageFile": str(package_file),
        "checkoutRoot": str(checkout_root) if checkout_root else None,
        "branch": branch,
        "commit": revision,
        "runtimeName": "python",
        "runtimeVersion": platform.python_version(),
        "osType": platform.system().lower(),
        "osVersion": platform.version(),
        "hostArch": platform.machine().lower(),
    }


def resource_attributes(run_id: str, identity: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "service.name": SERVICE_NAME,
        "benchmark.run_id": run_id,
        "benchmark.source": BENCHMARK_SOURCE,
        "package.name": identity["packageName"],
        "package.version": identity["packageVersion"],
        "vcs.revision": identity.get("commit") or "",
        "process.runtime.name": identity["runtimeName"],
        "process.runtime.version": identity["runtimeVersion"],
        "os.type": identity["osType"],
        "os.version": identity["osVersion"],
        "host.arch": identity["hostArch"],
    }


def common_point_attributes(case: str, statistic: str) -> dict[str, Any]:
    scenario = SCENARIOS[case]
    return {
        "benchmark.test": scenario.case,
        "benchmark.scenario": scenario.scenario,
        "benchmark.name": scenario.scenario,
        "benchmark.category": scenario.category,
        "benchmark.statistic": statistic,
        "benchmark.case": scenario.case,
        "benchmark.implementation": scenario.implementation,
    }


def parse_benchmark_results(document: Mapping[str, Any], cases: Sequence[str]) -> list[dict[str, Any]]:
    requested = set(cases)
    entries: dict[str, Mapping[str, Any]] = {}
    for entry in document.get("benchmarks", []):
        name = str(entry.get("name", ""))
        case = name[len("test_") :] if name.startswith("test_") else name
        if case in requested:
            if case in entries:
                raise ValueError(f"Duplicate pytest-benchmark result for {case}")
            entries[case] = entry

    missing = requested - entries.keys()
    if missing:
        raise ValueError(f"Missing pytest-benchmark results: {', '.join(sorted(missing))}")

    points = []
    for case in cases:
        entry = entries[case]
        stats = entry.get("stats") or {}
        median_seconds = float(stats.get("median", 0))
        if median_seconds <= 0:
            raise ValueError(f"Benchmark median must be positive for {case}")
        attributes = common_point_attributes(case, "median")
        attributes.update(
            {
                "benchmark.iterations": int(stats.get("iterations", 0)),
                "benchmark.rounds": int(stats.get("rounds", 0)),
                "benchmark.gating": SCENARIOS[case].gating,
                "benchmark.unit": "operations/s",
            }
        )
        points.append(
            {
                "metric": "microsoft.opentelemetry.benchmark.throughput",
                "unit": "{operation}/s",
                "value": 1.0 / median_seconds,
                "attributes": attributes,
            }
        )
    return points


def run_benchmarks(
    cases: Sequence[str],
    output_path: Path,
    minimum_rounds: int,
    maximum_time: float,
    warmup: str,
) -> dict[str, Any]:
    expression = " or ".join(f"test_{case}" for case in cases)
    command = [
        sys.executable,
        "-m",
        "pytest",
        "tests/perf/test_overhead.py",
        "--benchmark-only",
        f"--benchmark-min-rounds={minimum_rounds}",
        f"--benchmark-max-time={maximum_time}",
        f"--benchmark-warmup={warmup}",
        f"--benchmark-json={output_path}",
        "-k",
        expression,
    ]
    subprocess.run(command, cwd=REPO_ROOT, check=True)
    with output_path.open("r", encoding="utf-8") as result_file:
        document = json.load(result_file)
    if not isinstance(document, dict):
        raise ValueError("pytest-benchmark JSON root must be an object")
    return document


def measure_memory_trial(case: str, iterations: int, warmup_iterations: int) -> dict[str, int]:
    if iterations <= 0 or warmup_iterations < 0:
        raise ValueError("Memory iterations must be positive and warmup iterations cannot be negative")

    process = psutil.Process()
    tracemalloc.start()
    managed = create_managed_operation(case)
    gc_was_enabled = gc.isenabled()
    try:
        for _ in range(warmup_iterations):
            managed.operation()
        for _ in range(3):
            gc.collect()

        heap_before = tracemalloc.get_traced_memory()[0]
        rss_before = process.memory_info().rss

        if gc_was_enabled:
            gc.disable()
        for _ in range(iterations):
            managed.operation()
        heap_immediate = tracemalloc.get_traced_memory()[0]
        rss_immediate = process.memory_info().rss

        if gc_was_enabled:
            gc.enable()
        for _ in range(3):
            gc.collect()
        heap_retained = tracemalloc.get_traced_memory()[0]
        rss_retained = process.memory_info().rss

        return {
            "heap_used_increase": heap_immediate - heap_before,
            "retained_heap_increase": heap_retained - heap_before,
            "rss_increase": rss_immediate - rss_before,
            "rss_retained_increase": rss_retained - rss_before,
        }
    finally:
        if gc_was_enabled and not gc.isenabled():
            gc.enable()
        managed.shutdown()
        tracemalloc.stop()


def run_memory_trials(
    cases: Sequence[str],
    iterations: int,
    warmup_iterations: int,
    trials: int,
    temporary_directory: Path,
) -> dict[str, list[dict[str, int]]]:
    raw_trials: dict[str, list[dict[str, int]]] = {}
    for case in cases:
        raw_trials[case] = []
        for trial_index in range(trials):
            worker_output = temporary_directory / f"{case}-{trial_index}.json"
            command = [
                sys.executable,
                "-m",
                "perf_tools.export",
                "_memory-worker",
                "--case",
                case,
                "--iterations",
                str(iterations),
                "--warmup-iterations",
                str(warmup_iterations),
                "--output",
                str(worker_output),
            ]
            result = subprocess.run(
                command,
                cwd=REPO_ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode:
                raise RuntimeError(
                    f"Memory worker failed for {case} trial {trial_index + 1}: "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )
            with worker_output.open("r", encoding="utf-8") as result_file:
                raw_trials[case].append(json.load(result_file))
    return raw_trials


def aggregate_memory(
    raw_trials: Mapping[str, Sequence[Mapping[str, int]]],
    iterations: int,
    trials: int,
    noise_floor_bytes: int,
) -> tuple[dict[str, dict[str, float]], list[dict[str, Any]]]:
    aggregates: dict[str, dict[str, float]] = {}
    points: list[dict[str, Any]] = []
    for case, case_trials in raw_trials.items():
        if len(case_trials) != trials:
            raise ValueError(f"Expected {trials} memory trials for {case}, got {len(case_trials)}")

        aggregate = {}
        for field in ("heap_used_increase", "retained_heap_increase", "rss_increase"):
            signed_median = float(statistics.median(float(trial[field]) for trial in case_trials))
            aggregate[f"{field}_signed_median"] = signed_median
            aggregate[field] = signed_median if signed_median > noise_floor_bytes else 0.0
        aggregate["heap_used_increase_per_operation"] = aggregate["heap_used_increase"] / iterations
        aggregate["retained_heap_increase_per_operation"] = aggregate["retained_heap_increase"] / iterations
        aggregates[case] = aggregate

        attributes = common_point_attributes(case, "median_observed_increase")
        attributes.update(
            {
                "benchmark.memory_iterations": iterations,
                "benchmark.memory_trials": trials,
                "benchmark.memory_noise_floor_bytes": noise_floor_bytes,
            }
        )
        for field, metric_name, unit in _MEMORY_METRICS:
            points.append(
                {
                    "metric": metric_name,
                    "unit": unit,
                    "value": aggregate[field],
                    "attributes": dict(attributes),
                }
            )
    return aggregates, points


def _any_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


def _attributes(values: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [{"key": key, "value": _any_value(value)} for key, value in sorted(values.items())]


def build_otlp_payload(
    points: Sequence[Mapping[str, Any]],
    resources: Mapping[str, Any],
    timestamp_ns: int | None = None,
) -> dict[str, Any]:
    timestamp = str(timestamp_ns if timestamp_ns is not None else time.time_ns())
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for point in points:
        key = (str(point["metric"]), str(point["unit"]))
        grouped.setdefault(key, []).append(point)

    metrics = []
    for (name, unit), metric_points in grouped.items():
        metrics.append(
            {
                "name": name,
                "unit": unit,
                "gauge": {
                    "dataPoints": [
                        {
                            "attributes": _attributes(point["attributes"]),
                            "timeUnixNano": timestamp,
                            "asDouble": float(point["value"]),
                        }
                        for point in metric_points
                    ]
                },
            }
        )
    return {
        "resourceMetrics": [
            {
                "resource": {"attributes": _attributes(resources)},
                "scopeMetrics": [
                    {
                        "scope": {
                            "name": "microsoft.opentelemetry.performance",
                            "version": SCHEMA_VERSION,
                        },
                        "metrics": metrics,
                    }
                ],
            }
        ]
    }


def post_otlp_payload(endpoint: str, payload: Mapping[str, Any], timeout: float) -> None:
    safe_endpoint = redact_endpoint(endpoint)
    try:
        response = requests.post(
            endpoint,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException as error:
        raise RuntimeError(f"OTLP export request failed for {safe_endpoint}: {type(error).__name__}") from None
    if not response.ok:
        raise RuntimeError(f"OTLP export failed for {safe_endpoint} with HTTP {response.status_code}")


def write_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    with temporary_path.open("w", encoding="utf-8") as output_file:
        json.dump(document, output_file, indent=2, sort_keys=True)
        output_file.write("\n")
    temporary_path.replace(path)


def _validate_cases(cases: Sequence[str] | None) -> tuple[str, ...]:
    selected = tuple(cases or SCENARIO_ORDER)
    if len(selected) != len(set(selected)):
        raise ValueError("Benchmark cases cannot be repeated")
    return selected


def _run(args: argparse.Namespace) -> int:
    cases = _validate_cases(args.cases)
    if args.memory_iterations <= 0 or args.memory_trials <= 0 or args.memory_warmup_iterations < 0:
        raise ValueError("Memory iterations/trials must be positive and warmup iterations cannot be negative")
    if args.memory_noise_floor_bytes < 0:
        raise ValueError("Memory noise floor cannot be negative")

    endpoint = None if args.no_export else resolve_otlp_metrics_endpoint(args.otlp_endpoint)
    identity = collect_identity(require_export_identity=not args.no_export)
    run_id = args.run_id or str(uuid.uuid4())
    output_path = Path(args.output).resolve()

    with tempfile.TemporaryDirectory(prefix="otel-python-perf-") as temporary_name:
        temporary_directory = Path(temporary_name)
        benchmark_path = temporary_directory / "pytest-benchmark.json"
        benchmark_document = run_benchmarks(
            cases,
            benchmark_path,
            args.benchmark_min_rounds,
            args.benchmark_max_time,
            args.benchmark_warmup,
        )
        throughput_points = parse_benchmark_results(benchmark_document, cases)
        raw_trials = run_memory_trials(
            cases,
            args.memory_iterations,
            args.memory_warmup_iterations,
            args.memory_trials,
            temporary_directory,
        )

    memory_aggregates, memory_points = aggregate_memory(
        raw_trials,
        args.memory_iterations,
        args.memory_trials,
        args.memory_noise_floor_bytes,
    )
    points = [*throughput_points, *memory_points]
    resources = resource_attributes(run_id, identity)
    payload = build_otlp_payload(points, resources)
    document: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "language": "python",
        "source": BENCHMARK_SOURCE,
        "runId": run_id,
        "endpoint": redact_endpoint(endpoint),
        "identity": identity,
        "resourceAttributes": resources,
        "testScenarioMapping": test_scenario_mapping(),
        "metricContract": list(METRIC_CONTRACT),
        "configuration": {
            "benchmarkMinRounds": args.benchmark_min_rounds,
            "benchmarkMaxTimeSeconds": args.benchmark_max_time,
            "benchmarkWarmup": args.benchmark_warmup,
            "memoryIterations": args.memory_iterations,
            "memoryWarmupIterations": args.memory_warmup_iterations,
            "memoryTrials": args.memory_trials,
            "memoryNoiseFloorBytes": args.memory_noise_floor_bytes,
        },
        "pytestBenchmark": benchmark_document,
        "memory": {
            "semantics": (
                "tracemalloc current Python allocator bytes for immediate and post-GC retained heap; "
                "process RSS is sampled separately and is not a native allocation count or V8 heapUsed equivalent"
            ),
            "rawSignedTrials": raw_trials,
            "aggregates": memory_aggregates,
        },
        "points": points,
        "otlpPayload": payload,
        "export": {
            "attempted": not args.no_export,
            "succeeded": False if not args.no_export else None,
            "error": None,
        },
    }
    write_json(output_path, document)

    if endpoint:
        try:
            post_otlp_payload(endpoint, payload, args.export_timeout)
        except Exception as error:
            document["export"]["error"] = str(error)
            write_json(output_path, document)
            raise
        document["export"]["succeeded"] = True
        write_json(output_path, document)

    print(json.dumps({"runId": run_id, "output": str(output_path), "endpoint": redact_endpoint(endpoint)}))
    return 0


def _run_memory_worker(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--case", required=True, choices=SCENARIO_ORDER)
    parser.add_argument("--iterations", required=True, type=int)
    parser.add_argument("--warmup-iterations", required=True, type=int)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(arguments)
    result = measure_memory_trial(args.case, args.iterations, args.warmup_iterations)
    write_json(Path(args.output), result)
    return 0


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the existing Python distro performance cases, measure memory in isolated child processes, "
            "write a diagnostic JSON artifact, and optionally export OTLP/HTTP JSON Gauges."
        ),
        epilog=(
            "Every point sets benchmark.test to its exact pytest case "
            "(azure_monitor_span, azure_monitor_log, otel_span, or otel_log); "
            "benchmark.scenario and benchmark.name remain the shared span or logger_emit identity."
        ),
    )
    parser.add_argument("--output", required=True, help="Diagnostic JSON artifact path")
    parser.add_argument("--run-id", help="Benchmark run ID (default: a new UUID)")
    parser.add_argument(
        "--otlp-endpoint",
        help="OTLP base or metrics endpoint (fallback: OTEL_EXPORTER_OTLP_ENDPOINT)",
    )
    parser.add_argument("--no-export", action="store_true", help="Write JSON without sending OTLP metrics")
    parser.add_argument("--export-timeout", type=float, default=30.0, help="OTLP POST timeout in seconds")
    parser.add_argument(
        "--case",
        dest="cases",
        action="append",
        choices=SCENARIO_ORDER,
        help="Benchmark case to run; repeat as needed (default: all four)",
    )
    parser.add_argument("--benchmark-min-rounds", type=int, default=5)
    parser.add_argument("--benchmark-max-time", type=float, default=1.0)
    parser.add_argument("--benchmark-warmup", choices=("on", "off", "auto"), default="on")
    parser.add_argument("--memory-iterations", type=int, default=1000)
    parser.add_argument("--memory-warmup-iterations", type=int, default=100)
    parser.add_argument("--memory-trials", type=int, default=5)
    parser.add_argument("--memory-noise-floor-bytes", type=int, default=4096)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    if arguments and arguments[0] == "_memory-worker":
        return _run_memory_worker(arguments[1:])
    return _run(build_argument_parser().parse_args(arguments))


if __name__ == "__main__":
    raise SystemExit(main())
