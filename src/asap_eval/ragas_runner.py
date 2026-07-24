from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from pydantic import SecretStr
from ragas import evaluate
from ragas.dataset_schema import EvaluationDataset, SingleTurnSample
from ragas.embeddings.base import LangchainEmbeddingsWrapper
from ragas.llms.base import LangchainLLMWrapper
from ragas.metrics.base import ModeMetric
from ragas.run_config import RunConfig

from .config import EvalConfig, JudgeEnvironment, RagasConfig
from .metrics import RAGAS_METRIC_NAMES, build_ragas_metrics
from .models import InferenceRecord, InferenceStatus


@dataclass(frozen=True)
class PreparedRagasSample:
    sample_id: str
    source_row: int
    sample: SingleTurnSample


@dataclass(frozen=True)
class RagasRunResult:
    prepared_samples: list[PreparedRagasSample]
    scores_by_sample_id: dict[str, dict[str, float | None]]
    metric_names: list[str]


EvaluateFn = Callable[..., Any]


def build_run_config(config: RagasConfig) -> RunConfig:
    return RunConfig(
        timeout=config.timeout_seconds,
        max_retries=config.max_retries,
        max_wait=config.max_wait_seconds,
        max_workers=config.max_workers,
        seed=config.seed,
    )


def build_judge_wrappers(
    judge: JudgeEnvironment,
    run_config: RunConfig,
) -> tuple[LangchainLLMWrapper, LangchainEmbeddingsWrapper]:
    _require_judge_environment(judge)
    llm = ChatOpenAI(
        api_key=_secret_value(judge.llm_api_key),
        base_url=judge.llm_base_url,
        model=judge.llm_model_name,
        temperature=0,
    )
    embeddings = OpenAIEmbeddings(
        api_key=_secret_value(judge.embed_api_key),
        base_url=judge.embed_base_url,
        model=judge.embed_model_name,
    )
    return (
        LangchainLLMWrapper(llm, run_config=run_config),
        LangchainEmbeddingsWrapper(embeddings, run_config=run_config),
    )


def prepare_ragas_samples(records: Sequence[InferenceRecord]) -> list[PreparedRagasSample]:
    prepared: list[PreparedRagasSample] = []
    for record in records:
        if record.status != InferenceStatus.OK or not record.answer:
            continue
        prepared.append(
            PreparedRagasSample(
                sample_id=record.sample_id,
                source_row=record.source.source_row,
                sample=SingleTurnSample(
                    user_input=record.source.query_text,
                    response=record.answer,
                    retrieved_contexts=[
                        context.text for context in record.retrieved_contexts
                    ],
                    reference=record.source.answer,
                    reference_contexts=[record.source.chunk_text],
                ),
            )
        )
    return prepared


def run_ragas_evaluation(
    records: Sequence[InferenceRecord],
    config: EvalConfig,
    *,
    llm: Any | None = None,
    embeddings: Any | None = None,
    evaluate_fn: EvaluateFn = evaluate,
) -> RagasRunResult:
    prepared = prepare_ragas_samples(records)
    metric_instances = build_ragas_metrics()
    metric_names = [metric.name for metric in metric_instances]
    score_keys = [_ragas_score_key(metric) for metric in metric_instances]
    if metric_names != RAGAS_METRIC_NAMES:
        raise RuntimeError(f"Unexpected RAGAS metric order: {metric_names}")

    run_config = build_run_config(config.ragas)
    if llm is None or embeddings is None:
        llm, embeddings = build_judge_wrappers(config.judge, run_config)

    if not prepared:
        return RagasRunResult(
            prepared_samples=[],
            scores_by_sample_id={},
            metric_names=metric_names,
        )

    result = evaluate_fn(
        EvaluationDataset(samples=[item.sample for item in prepared]),
        metrics=metric_instances,
        llm=llm,
        embeddings=embeddings,
        run_config=run_config,
        raise_exceptions=config.ragas.raise_exceptions,
        batch_size=config.ragas.batch_size,
    )
    score_rows = _extract_ordered_score_rows(result)
    if len(score_rows) != len(prepared):
        raise ValueError(
            "RAGAS returned a different number of score rows than input samples: "
            f"{len(score_rows)} != {len(prepared)}"
        )

    scores_by_sample_id: dict[str, dict[str, float | None]] = {}
    for prepared_sample, score_row in zip(prepared, score_rows, strict=True):
        scores_by_sample_id[prepared_sample.sample_id] = {
            metric_name: _score_value(score_row.get(score_key))
            for metric_name, score_key in zip(metric_names, score_keys, strict=True)
        }

    return RagasRunResult(
        prepared_samples=prepared,
        scores_by_sample_id=scores_by_sample_id,
        metric_names=metric_names,
    )


def _extract_ordered_score_rows(result: Any) -> list[dict[str, Any]]:
    scores = getattr(result, "scores", None)
    if scores is not None:
        return [dict(row) for row in scores]
    if isinstance(result, list):
        return [dict(row) for row in result]
    if isinstance(result, dict) and "scores" in result:
        return [dict(row) for row in result["scores"]]
    if hasattr(result, "to_pandas"):
        frame = result.to_pandas()
        return [dict(row) for row in frame.to_dict(orient="records")]
    raise TypeError(f"Unsupported RAGAS result type: {type(result).__name__}")


def _ragas_score_key(metric: Any) -> str:
    if isinstance(metric, ModeMetric):
        return f"{metric.name}(mode={metric.mode})"
    return str(metric.name)


def _score_value(value: Any) -> float | None:
    if value is None:
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(score) else score


def _require_judge_environment(judge: JudgeEnvironment) -> None:
    missing = [
        name
        for name, value in {
            "JUDGE_LLM_API_KEY": judge.llm_api_key,
            "JUDGE_LLM_BASE_URL": judge.llm_base_url,
            "JUDGE_LLM_MODEL_NAME": judge.llm_model_name,
            "JUDGE_EMBED_API_KEY": judge.embed_api_key,
            "JUDGE_EMBED_BASE_URL": judge.embed_base_url,
            "JUDGE_EMBED_MODEL_NAME": judge.embed_model_name,
        }.items()
        if not value
    ]
    if missing:
        raise ValueError(
            "Missing judge environment variables required for RAGAS evaluation: "
            + ", ".join(missing)
        )


def _secret_value(value: SecretStr | None) -> str:
    if value is None:
        return ""
    return value.get_secret_value()
