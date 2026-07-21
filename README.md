# RAG+ASAP evaluation suite

This project collects structured answers from the `rag_tool_asap` MCP tool and evaluates
them with RAGAS `0.3.2` plus two deterministic retrieval-hit metrics.

## Dataset

Use the component fixture as the canonical dataset:

```text
../self-service-orig/services/components/rag_tool_asap/tests/files/asap.csv
```

Expected audit counts:

- raw rows: 10,000
- distinct chunks: 1,867
- query-bearing rows: 1,843
- document-only rows: 8,157
- distinct query IDs: 1,825

The benchmark denominator is the 1,843 query-bearing source rows. Duplicate and ambiguous
`query_id` values are preserved and reported; scores are joined by stable sample ID/source row,
not by question text.

## Configuration

Copy the example files:

```bash
cp config.example.toml config.toml
cp .env.example .env
```

Judge credentials are read from the environment, not from TOML artifacts:

```text
JUDGE_LLM_API_KEY
JUDGE_LLM_BASE_URL
JUDGE_LLM_MODEL_NAME
JUDGE_EMBED_API_KEY
JUDGE_EMBED_BASE_URL
JUDGE_EMBED_MODEL_NAME
```

`inference.max_concurrency` controls concurrent MCP calls. `ragas.max_workers` controls
RAGAS judge concurrency. These target different services.

## Commands

Audit the dataset:

```bash
uv sync --frozen
uv run asap-eval audit --config config.toml
```

Collect a small smoke run after the component MCP server is ready:

```bash
uv run asap-eval collect --config config.toml --max-samples 5
```

Run RAGAS on an existing collection without calling the component again:

```bash
uv run asap-eval evaluate --config config.toml --run-dir results/<run-id>
```

Run collection and judging together:

```bash
uv run asap-eval run --config config.toml --max-samples 5 --max-workers 2
```

For the full baseline, omit `--max-samples` after validating smoke artifacts.

## Artifacts

Each collection creates `results/<UTC timestamp>-<short dataset hash>/` containing:

- `run_manifest.json` — sanitized config, dataset audit, MCP preflight, RAGAS version,
  metric names, component git state, and model names. API keys are redacted.
- `inference_samples.jsonl` — one checkpointed nested inference record per source query row.
- `ragas_input.jsonl` — the ordered `SingleTurnSample` payloads sent to RAGAS.
- `scores.csv` — per-sample flat score table with the five RAGAS metrics, `context_hit`,
  `title_hit`, status, and source identity.
- `scores.jsonl` — per-sample score records joined to the full nested inference trace.
- `summary.json` and `summary.md` — aggregate means, valid/NaN/failure counts, and
  deterministic retrieval accuracies.

Resume behavior is based on `sample_id`. Completed records in `inference_samples.jsonl` are
not reissued unless `--overwrite` is passed to `collect` or `run`.

RAGAS judging is usually more expensive than answer collection. Validate a 5- or 10-sample
smoke run before collecting and judging all 1,843 queries.
