from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from asap_eval.config import EvalConfig, RagasConfig
from asap_eval.metrics import RAGAS_METRIC_NAMES, build_ragas_metrics
from asap_eval.models import InferenceRecord, InferenceStatus, SourceReference
from asap_eval.ragas_runner import prepare_ragas_samples, run_ragas_evaluation


def make_record(
    sample_id: str,
    *,
    query_text: str = "Question?",
    answer: str | None = "Answer",
    status: InferenceStatus = InferenceStatus.OK,
) -> InferenceRecord:
    return InferenceRecord(
        sample_id=sample_id,
        dataset_sha256="d" * 64,
        source=SourceReference(
            source_row=int(sample_id.rsplit("-", 1)[-1]),
            query_id="same-query-id",
            query_text=query_text,
            answer="Reference answer",
            chunk_id="chunk-1",
            chunk_text="Reference context",
            doc_title="Doc",
        ),
        status=status,
        answer=answer,
        retrieved_contexts=[],
        demonstrations=[],
        latency_seconds=0.1,
        attempts=1,
    )


def test_five_metric_classes_and_names_match_ragas_032() -> None:
    metrics = build_ragas_metrics()
    assert [metric.__class__.__name__ for metric in metrics] == [
        "Faithfulness",
        "AnswerAccuracy",
        "ResponseGroundedness",
        "ContextRelevance",
        "FactualCorrectness",
    ]
    assert [metric.name for metric in metrics] == RAGAS_METRIC_NAMES


def test_prepare_ragas_samples_maps_all_requested_fields() -> None:
    record = make_record("sample-1")
    record.retrieved_contexts = [
        SimpleNamespace(text="Retrieved A"),
        SimpleNamespace(text="Retrieved B"),
    ]

    prepared = prepare_ragas_samples([record])

    assert len(prepared) == 1
    payload = prepared[0].sample.model_dump()
    assert payload["user_input"] == "Question?"
    assert payload["response"] == "Answer"
    assert payload["retrieved_contexts"] == ["Retrieved A", "Retrieved B"]
    assert payload["reference"] == "Reference answer"
    assert payload["reference_contexts"] == ["Reference context"]


def test_evaluate_receives_wrappers_batch_size_and_run_config_max_workers() -> None:
    config = EvalConfig(
        dataset_path=Path("dataset.csv"),
        ragas=RagasConfig(max_workers=13, batch_size=7),
    )
    calls: dict[str, Any] = {}

    def fake_evaluate(dataset: Any, **kwargs: Any) -> Any:
        calls["dataset"] = dataset
        calls.update(kwargs)
        return SimpleNamespace(
            scores=[
                {metric_name: 0.5 for metric_name in RAGAS_METRIC_NAMES},
            ]
        )

    llm = object()
    embeddings = object()
    result = run_ragas_evaluation(
        [make_record("sample-1")],
        config,
        llm=llm,
        embeddings=embeddings,
        evaluate_fn=fake_evaluate,
    )

    assert calls["llm"] is llm
    assert calls["embeddings"] is embeddings
    assert calls["batch_size"] == 7
    assert calls["run_config"].max_workers == 13
    assert calls["raise_exceptions"] is False
    assert len(calls["dataset"].samples) == 1
    assert result.scores_by_sample_id["sample-1"]["faithfulness"] == 0.5


def test_duplicate_question_texts_join_scores_by_ordered_sample_index() -> None:
    config = EvalConfig(dataset_path=Path("dataset.csv"))
    records = [
        make_record("sample-1", query_text="Same question?"),
        make_record("sample-2", query_text="Same question?"),
    ]

    def fake_evaluate(dataset: Any, **kwargs: Any) -> Any:
        assert [sample.user_input for sample in dataset.samples] == [
            "Same question?",
            "Same question?",
        ]
        return SimpleNamespace(
            scores=[
                {metric_name: 0.1 for metric_name in RAGAS_METRIC_NAMES},
                {metric_name: 0.9 for metric_name in RAGAS_METRIC_NAMES},
            ]
        )

    result = run_ragas_evaluation(
        records,
        config,
        llm=object(),
        embeddings=object(),
        evaluate_fn=fake_evaluate,
    )

    assert result.scores_by_sample_id["sample-1"]["faithfulness"] == 0.1
    assert result.scores_by_sample_id["sample-2"]["faithfulness"] == 0.9


def test_mode_metric_score_key_maps_to_canonical_metric_name() -> None:
    config = EvalConfig(dataset_path=Path("dataset.csv"))

    def fake_evaluate(dataset: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(
            scores=[
                {
                    "faithfulness": 1.0,
                    "nv_accuracy": 1.0,
                    "nv_response_groundedness": 1.0,
                    "nv_context_relevance": 1.0,
                    "factual_correctness(mode=f1)": 0.67,
                }
            ]
        )

    result = run_ragas_evaluation(
        [make_record("sample-1")],
        config,
        llm=object(),
        embeddings=object(),
        evaluate_fn=fake_evaluate,
    )

    assert result.scores_by_sample_id["sample-1"]["factual_correctness"] == 0.67
