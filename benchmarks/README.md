# Phase 5 benchmark harness

This directory measures the current `/compare` request without changing the comparison algorithm or render quality.

## Generate synthetic CAD-like fixtures

```bash
.venv/bin/python benchmarks/generate_fixtures.py \
  --output /tmp/pdf-diff-phase5-fixtures
```

Fixtures cover one-page A4, ten-page A4, one-page A1 and one-page A0. Every size has an identical pair and a pair with small line/text changes. Generated PDFs stay outside Git.

## Run the baseline

```bash
.venv/bin/python benchmarks/run_baseline.py \
  --fixtures /tmp/pdf-diff-phase5-fixtures \
  --output benchmarks/results/phase5-baseline.json \
  --repeats 2 \
  --timeout 300
```

Each repeat runs in a fresh process under `/usr/bin/time -v`. The worker performs a complete Flask `/compare` multipart request, so validation, temporary upload copies, PDF rendering, comparison, PNG encoding and HTML rendering are included.

Measured values:

- request wall time;
- first-result time;
- peak RSS;
- user/system CPU time;
- full HTML response size;
- unique PNG count, total size and maximum size;
- input size and HTTP status.

The current endpoint is fully buffered. Therefore first-result time equals wall time: the browser receives no result bytes until every page and the complete HTML have been generated.

## Reproducibility notes

- Run on an otherwise idle host.
- `OMP_NUM_THREADS=1` and `OPENBLAS_NUM_THREADS=1` are set for every worker.
- Keep the current `RENDER_ZOOM = 3.0` when comparing with the checked-in baseline.
- Synthetic fixtures are deterministic, but they do not replace a later benchmark on approved real CAD drawings.
- A0 comparisons can consume nearly 2 GiB RSS. Do not run them concurrently with other heavy jobs.
