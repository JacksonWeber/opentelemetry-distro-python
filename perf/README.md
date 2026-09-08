# Performance benchmarks

This directory contains the regression-gating glue used by the `performance`
CI workflow. The benchmarks themselves live in
[`tests/perf/test_overhead.py`](../tests/perf/test_overhead.py) and are
driven by [`pytest-benchmark`](https://pytest-benchmark.readthedocs.io/).

## Scenarios

| Test | Gating | What it measures |
| --- | --- | --- |
| `test_azure_monitor_span` | yes | `configure_azure_monitor` + `tracer.start_as_current_span` |
| `test_azure_monitor_log`  | yes | `configure_azure_monitor` + `logger.info` |
| `test_otel_span`          | no  | Plain `opentelemetry-sdk` `TracerProvider` reference |
| `test_otel_log`           | no  | Plain `opentelemetry-sdk` `LoggerProvider` reference |

Non-gating scenarios are informational only — they show how much overhead
the SDK adds on top of upstream and never fail CI.

The gating flag is attached to each benchmark via
`benchmark.extra_info["gating"]` so `perf/compare.py` can pick it up out of
the pytest-benchmark JSON.

## Running locally

From the repo root, with `microsoft-opentelemetry` and dev deps installed
(`pip install -e . && pip install -r dev_requirements.txt`):

```bash
# Run the benchmarks and save the result.
pytest tests/perf --benchmark-only --benchmark-json=pr.json

# Compare against a previously-saved baseline (e.g. from main).
python -m perf.compare --baseline base.json --candidate pr.json --threshold 15
```

`pytest --benchmark-skip` is the default (see `pyproject.toml`), so a normal
`pytest` invocation will *skip* the perf tests entirely. Pass
`--benchmark-only` to opt in.

### Exporting a local OTLP performance run

The opt-in exporter runs the same four benchmark cases in a completed pytest
subprocess, then measures memory in separate child processes. Export setup and
network I/O therefore do not enter the timed throughput loops.
All scenarios retain their configured samplers and processors but use local
discard transports so background network retries and in-memory export storage
cannot perturb timings or memory results.

```bash
python -m perf.export \
  --otlp-endpoint https://collector.example/otlp \
  --output python-perf-result.json
```

`OTEL_EXPORTER_OTLP_ENDPOINT` can be used instead of `--otlp-endpoint`. The
exporter appends `/v1/metrics` to the configured OTLP base and sends OTLP/HTTP
JSON with `Content-Type: application/json`. Use `--no-export` to create only
the diagnostic JSON artifact. Run `python -m perf.export --help` for bounded
throughput, case-selection, memory-iteration, trial, and noise-floor options.
Every point uses the exact pytest case (`azure_monitor_span`,
`azure_monitor_log`, `otel_span`, or `otel_log`) as `benchmark.test`.
`benchmark.scenario` and `benchmark.name` use the shared `span` or
`logger_emit` identity so test and scenario remain independently queryable.

Memory trials use fresh child processes, warm up each operation, stabilize
garbage collection, and report robust median observed increases. Raw signed
trial values are retained only in the diagnostic JSON. Exported values at or
below the default 4096-byte noise floor are zero and are never negative.
Python heap values come from `tracemalloc`, so they represent traced Python
allocator memory rather than V8 `heapUsed` or native allocation counts. RSS is
measured separately for whole-process growth.

## CI

`.github/workflows/performance.yml` runs the suite on every pull request:

1. Install the PR branch, run `pytest tests/perf --benchmark-only
   --benchmark-json=pr.json`.
2. Check out the base branch, install it, repeat → `base.json`.
3. `perf/compare.py` produces a markdown report and exits non-zero if any
   *gating* scenario regresses by more than `PERF_REGRESSION_THRESHOLD`
   percent (default 15).
4. The report is posted as a sticky PR comment.
