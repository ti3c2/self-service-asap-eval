from __future__ import annotations

import math
from collections import Counter
from typing import Any

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


def custom_metric_columns(record: InferenceRecord) -> dict[str, int]:
    return {
        "context_hit": context_hit(record),
        "title_hit": title_hit(record),
    }


def custom_metric_summary(records: list[InferenceRecord]) -> dict[str, Any]:
    denominator = len(records)
    context_numerator = sum(context_hit(record) for record in records)
    title_numerator = sum(title_hit(record) for record in records)
    return {
        "context_accuracy": context_numerator / denominator if denominator else math.nan,
        "context_hit_numerator": context_numerator,
        "title_accuracy": title_numerator / denominator if denominator else math.nan,
        "title_hit_numerator": title_numerator,
        "denominator": denominator,
    }


def collection_status_counts(records: list[InferenceRecord]) -> dict[str, int]:
    return dict(Counter(record.status.value for record in records))
