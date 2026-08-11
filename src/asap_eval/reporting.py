from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .artifacts import atomic_write_csv, atomic_write_json, write_jsonl_atomic
from .dataset import DatasetAudit
from .metrics import (
    CUSTOM_METRIC_FIELDNAMES,
    collection_status_counts,
    custom_metric_columns_by_sample_id,
    custom_metric_summary,
)
from .models import InferenceRecord
from .ragas_runner import PreparedRagasSample, RagasRunResult


def write_evaluation_artifacts(
    run_dir: str | Path,
    records: list[InferenceRecord],
    ragas_result: RagasRunResult,
    *,
    audit: DatasetAudit | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> dict[str, Any]:
    run_path = Path(run_dir)
    run_path.mkdir(parents=True, exist_ok=True)
    started = started_at or datetime.now(timezone.utc)
    finished = finished_at or datetime.now(timezone.utc)

    _write_ragas_input(run_path / "ragas_input.jsonl", ragas_result.prepared_samples)
    score_rows = build_score_rows(records, ragas_result)
    score_fieldnames = [
        "sample_id",
        "source_row",
        "query_id",
        "chunk_id",
        "doc_title",
        "status",
        "error",
        *ragas_result.metric_names,
        *CUSTOM_METRIC_FIELDNAMES,
    ]
    atomic_write_csv(run_path / "scores.csv", score_rows, score_fieldnames)
    write_jsonl_atomic(run_path / "scores.jsonl", build_score_jsonl(records, ragas_result))

    summary = build_summary(
        records,
        ragas_result,
        audit=audit,
        started_at=started,
        finished_at=finished,
    )
    atomic_write_json(run_path / "summary.json", summary)
    (run_path / "summary.md").write_text(summary_markdown(summary), encoding="utf-8")
    return summary


def build_score_rows(
    records: list[InferenceRecord],
    ragas_result: RagasRunResult,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    custom_rows = custom_metric_columns_by_sample_id(records)
    for record in records:
        metric_values = ragas_result.scores_by_sample_id.get(record.sample_id, {})
        row = {
            "sample_id": record.sample_id,
            "source_row": record.source.source_row,
            "query_id": record.source.query_id,
            "chunk_id": record.source.chunk_id,
            "doc_title": record.source.doc_title,
            "status": record.status.value,
            "error": record.error or "",
            **{
                metric_name: _csv_score(metric_values.get(metric_name))
                for metric_name in ragas_result.metric_names
            },
            **custom_rows[record.sample_id],
        }
        rows.append(row)
    return rows


def build_score_jsonl(
    records: list[InferenceRecord],
    ragas_result: RagasRunResult,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    custom_rows = custom_metric_columns_by_sample_id(records)
    for record in records:
        metric_values = ragas_result.scores_by_sample_id.get(record.sample_id, {})
        output.append(
            {
                "sample_id": record.sample_id,
                "source": record.source.model_dump(mode="json"),
                "inference": record.model_dump(mode="json"),
                "ragas_scores": {
                    metric_name: metric_values.get(metric_name)
                    for metric_name in ragas_result.metric_names
                },
                **custom_rows[record.sample_id],
            }
        )
    return output


def build_summary(
    records: list[InferenceRecord],
    ragas_result: RagasRunResult,
    *,
    audit: DatasetAudit | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> dict[str, Any]:
    started = started_at or datetime.now(timezone.utc)
    finished = finished_at or datetime.now(timezone.utc)
    return {
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "duration_seconds": max(0.0, (finished - started).total_seconds()),
        "total_samples": len(records),
        "collection_status_counts": collection_status_counts(records),
        "ragas_eligible_count": len(ragas_result.prepared_samples),
        "ragas_scored_count": len(ragas_result.scores_by_sample_id),
        "ragas_metrics": _ragas_metric_summary(records, ragas_result),
        "custom_metrics": custom_metric_summary(records),
        "audit": audit.model_dump(mode="json") if audit is not None else None,
        "source_rows": {
            "min": min((record.source.source_row for record in records), default=None),
            "max": max((record.source.source_row for record in records), default=None),
        },
    }


def summary_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# RAG+ASAP evaluation summary",
        "",
        f"- Total samples: {summary['total_samples']}",
        f"- RAGAS eligible samples: {summary['ragas_eligible_count']}",
        f"- RAGAS scored samples: {summary['ragas_scored_count']}",
        f"- Collection status counts: {summary['collection_status_counts']}",
        "",
        "## Retrieval metrics",
        "",
    ]
    custom = summary["custom_metrics"]
    lines.extend(
        [
            f"- Context accuracy: {_format_score(custom['context_accuracy'])} "
            f"({custom['context_hit_numerator']}/{custom['denominator']})",
            f"- Context MRR: {_format_score(custom['context_mrr'])}",
            f"- Context nDCG: {_format_score(custom['context_ndcg'])}",
            f"- Title accuracy: {_format_score(custom['title_accuracy'])} "
            f"({custom['title_hit_numerator']}/{custom['denominator']})",
            "",
            "## RAGAS metrics",
            "",
        ]
    )
    for metric_name, values in summary["ragas_metrics"].items():
        lines.append(
            f"- {metric_name}: mean={_format_score(values['mean'])}, "
            f"valid={values['valid_count']}, nan={values['nan_count']}, "
            f"failed={values['failure_count']}"
        )
    audit = summary.get("audit")
    if audit:
        lines.extend(
            [
                "",
                "## Dataset audit",
                "",
                f"- Dataset SHA-256: `{audit['dataset_sha256']}`",
                f"- Raw rows: {audit['raw_rows']}",
                f"- Query rows: {audit['query_rows']}",
                f"- Distinct chunks: {audit['distinct_chunks']}",
                f"- Repeated-query excess rows: {audit['repeated_query_excess_rows']}",
                f"- Conflicting query IDs: {audit['conflicting_query_ids']}",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def _write_ragas_input(
    path: Path,
    prepared_samples: list[PreparedRagasSample],
) -> None:
    write_jsonl_atomic(
        path,
        [
            {
                "sample_id": prepared.sample_id,
                "source_row": prepared.source_row,
                "payload": prepared.sample.model_dump(mode="json"),
            }
            for prepared in prepared_samples
        ],
    )


def _ragas_metric_summary(
    records: list[InferenceRecord],
    ragas_result: RagasRunResult,
) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for metric_name in ragas_result.metric_names:
        valid_values: list[float] = []
        nan_count = 0
        failure_count = 0
        for record in records:
            score_row = ragas_result.scores_by_sample_id.get(record.sample_id)
            if score_row is None:
                failure_count += 1
                continue
            value = score_row.get(metric_name)
            if value is None:
                nan_count += 1
            else:
                valid_values.append(float(value))
        output[metric_name] = {
            "mean": sum(valid_values) / len(valid_values) if valid_values else None,
            "valid_count": len(valid_values),
            "nan_count": nan_count,
            "failure_count": failure_count,
        }
    return output


def _csv_score(value: float | None) -> str | float:
    return "" if value is None else value


def _format_score(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if math.isnan(number):
        return "nan"
    return f"{number:.4f}"
