from __future__ import annotations

from pathlib import Path

from ragas.dataset_schema import SingleTurnSample

from asap_eval.metrics import RAGAS_METRIC_NAMES, custom_metric_summary
from asap_eval.models import (
    InferenceRecord,
    InferenceStatus,
    RetrievedContext,
    SourceReference,
)
from asap_eval.ragas_runner import PreparedRagasSample, RagasRunResult
from asap_eval.reporting import build_summary, write_evaluation_artifacts


def make_record(
    sample_id: str,
    *,
    status: InferenceStatus = InferenceStatus.OK,
    contexts: list[RetrievedContext] | None = None,
) -> InferenceRecord:
    index = int(sample_id.rsplit("-", 1)[-1])
    return InferenceRecord(
        sample_id=sample_id,
        dataset_sha256="d" * 64,
        source=SourceReference(
            source_row=index,
            query_id=f"query-{index}",
            query_text=f"Question {index}?",
            answer=f"Reference {index}",
            chunk_id="chunk-hit",
            chunk_text="Reference context",
            doc_title="Exact Title",
        ),
        status=status,
        answer="Answer" if status == InferenceStatus.OK else None,
        error=None if status == InferenceStatus.OK else "failed",
        retrieved_contexts=contexts or [],
        demonstrations=[],
        latency_seconds=0.1,
        attempts=1,
    )


def context(chunk_id: str, title: str, position: int) -> RetrievedContext:
    return RetrievedContext(
        text=f"Context {position}",
        chunk_id=chunk_id,
        scoped_chunk_id=f"hash:{chunk_id}",
        doc_title=title,
        doc_hash="hash",
        prompt_position=position,
        synthetic_id="syn",
        synthetic_rank=1,
        context_rank=position,
    )


def test_context_and_title_hits_cover_hits_misses_duplicates_empty_and_failures() -> None:
    records = [
        make_record(
            "sample-1",
            contexts=[
                context("miss", "Wrong", 1),
                context("chunk-hit", "Wrong", 2),
                context("chunk-hit", "Exact Title", 3),
            ],
        ),
        make_record("sample-2", contexts=[context("miss", "Exact Title", 1)]),
        make_record("sample-3", contexts=[]),
        make_record("sample-4", status=InferenceStatus.ERROR, contexts=[]),
    ]

    summary = custom_metric_summary(records)

    assert summary["denominator"] == 4
    assert summary["context_hit_numerator"] == 1
    assert summary["title_hit_numerator"] == 2
    assert summary["context_accuracy"] == 0.25
    assert summary["title_accuracy"] == 0.5


def test_summary_exposes_nan_and_failure_counts() -> None:
    records = [
        make_record("sample-1", contexts=[context("chunk-hit", "Exact Title", 1)]),
        make_record("sample-2", contexts=[context("miss", "Wrong", 1)]),
        make_record("sample-3", status=InferenceStatus.ERROR),
    ]
    result = RagasRunResult(
        prepared_samples=[
            PreparedRagasSample(
                sample_id="sample-1",
                source_row=1,
                sample=SingleTurnSample(user_input="q", response="a"),
            ),
            PreparedRagasSample(
                sample_id="sample-2",
                source_row=2,
                sample=SingleTurnSample(user_input="q", response="a"),
            ),
        ],
        scores_by_sample_id={
            "sample-1": {metric_name: 1.0 for metric_name in RAGAS_METRIC_NAMES},
            "sample-2": {metric_name: None for metric_name in RAGAS_METRIC_NAMES},
        },
        metric_names=RAGAS_METRIC_NAMES,
    )

    summary = build_summary(records, result)

    assert summary["ragas_metrics"]["faithfulness"] == {
        "mean": 1.0,
        "valid_count": 1,
        "nan_count": 1,
        "failure_count": 1,
    }


def test_tiny_fake_end_to_end_writes_complete_artifact_set(tmp_path: Path) -> None:
    records = [
        make_record("sample-1", contexts=[context("chunk-hit", "Exact Title", 1)]),
        make_record("sample-2", status=InferenceStatus.ERROR),
    ]
    result = RagasRunResult(
        prepared_samples=[
            PreparedRagasSample(
                sample_id="sample-1",
                source_row=1,
                sample=SingleTurnSample(
                    user_input="Question?",
                    response="Answer",
                    retrieved_contexts=["Context"],
                    reference="Reference",
                    reference_contexts=["Reference context"],
                ),
            )
        ],
        scores_by_sample_id={
            "sample-1": {metric_name: 0.5 for metric_name in RAGAS_METRIC_NAMES}
        },
        metric_names=RAGAS_METRIC_NAMES,
    )

    write_evaluation_artifacts(tmp_path, records, result)

    expected = {
        "ragas_input.jsonl",
        "scores.csv",
        "scores.jsonl",
        "summary.json",
        "summary.md",
    }
    assert expected <= {path.name for path in tmp_path.iterdir()}
    assert "sample-1" in (tmp_path / "scores.csv").read_text(encoding="utf-8")
