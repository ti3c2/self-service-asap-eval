from __future__ import annotations

import math
from collections import Counter
from typing import Any

import ir_measures
from ir_measures import RR, nDCG
from ragas.metrics import (
    AnswerAccuracy,
    ContextRelevance,
    FactualCorrectness,
    Faithfulness,
    ResponseGroundedness,
)

from .models import InferenceRecord

RAGAS_METRIC_NAMES = [
    "faithfulness",
    "nv_accuracy",
    "nv_response_groundedness",
    "nv_context_relevance",
    "factual_correctness",
]


def build_ragas_metrics() -> list[Any]:
    return [
        Faithfulness(),
        AnswerAccuracy(),
        ResponseGroundedness(),
        ContextRelevance(),
        FactualCorrectness(language="english", mode="f1"),
    ]


def context_hit(record: InferenceRecord) -> int:
    reference_chunk_id = record.source.chunk_id
    return int(
        any(context.chunk_id == reference_chunk_id for context in record.retrieved_contexts)
    )


def title_hit(record: InferenceRecord) -> int:
    reference_title = record.source.doc_title.strip()
    return int(
        any(context.doc_title.strip() == reference_title for context in record.retrieved_contexts)
    )


CUSTOM_METRIC_FIELDNAMES = [
    "context_hit",
    "title_hit",
    "context_reciprocal_rank",
    "context_ndcg",
]


def custom_metric_columns(record: InferenceRecord) -> dict[str, int | float]:
    return custom_metric_columns_by_sample_id([record])[record.sample_id]


def custom_metric_columns_by_sample_id(
    records: list[InferenceRecord],
) -> dict[str, dict[str, int | float]]:
    context_ranking_metrics = _context_ranking_metrics_by_sample_id(records)
    return {
        record.sample_id: {
            "context_hit": context_hit(record),
            "title_hit": title_hit(record),
            **context_ranking_metrics[record.sample_id],
        }
        for record in records
    }


def custom_metric_summary(records: list[InferenceRecord]) -> dict[str, Any]:
    denominator = len(records)
    rows_by_sample_id = custom_metric_columns_by_sample_id(records)
    context_numerator = sum(
        int(row["context_hit"]) for row in rows_by_sample_id.values()
    )
    title_numerator = sum(int(row["title_hit"]) for row in rows_by_sample_id.values())
    reciprocal_rank_sum = sum(
        float(row["context_reciprocal_rank"])
        for row in rows_by_sample_id.values()
    )
    ndcg_sum = sum(float(row["context_ndcg"]) for row in rows_by_sample_id.values())
    return {
        "context_accuracy": context_numerator / denominator if denominator else math.nan,
        "context_hit_numerator": context_numerator,
        "context_mrr": reciprocal_rank_sum / denominator if denominator else math.nan,
        "context_ndcg": ndcg_sum / denominator if denominator else math.nan,
        "title_accuracy": title_numerator / denominator if denominator else math.nan,
        "title_hit_numerator": title_numerator,
        "denominator": denominator,
    }


def collection_status_counts(records: list[InferenceRecord]) -> dict[str, int]:
    return dict(Counter(record.status.value for record in records))


def _context_ranking_metrics_by_sample_id(
    records: list[InferenceRecord],
) -> dict[str, dict[str, float]]:
    if not records:
        return {}

    qrels: dict[str, dict[str, int]] = {}
    run: dict[str, dict[str, float]] = {}
    for record in records:
        qrels[record.sample_id] = {record.source.chunk_id: 1}
        run[record.sample_id] = _run_scores_from_ranked_contexts(record)

    output = {
        record.sample_id: {
            "context_reciprocal_rank": 0.0,
            "context_ndcg": 0.0,
        }
        for record in records
    }
    for metric in ir_measures.iter_calc([RR, nDCG], qrels, run):
        column = _IR_MEASURE_COLUMNS[str(metric.measure)]
        output[metric.query_id][column] = float(metric.value)
    return output


_IR_MEASURE_COLUMNS = {
    "RR": "context_reciprocal_rank",
    "nDCG": "context_ndcg",
}


def _run_scores_from_ranked_contexts(record: InferenceRecord) -> dict[str, float]:
    scores: dict[str, float] = {}
    total_contexts = len(record.retrieved_contexts)
    for index, context in enumerate(record.retrieved_contexts):
        if context.chunk_id in scores:
            continue
        scores[context.chunk_id] = float(total_contexts - index)
    return scores
