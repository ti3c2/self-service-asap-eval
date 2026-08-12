from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FlexibleBaseModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class SourceReference(StrictBaseModel):
    source_row: int
    query_id: str
    query_text: str
    answer: str
    chunk_id: str
    chunk_text: str
    doc_title: str


class DatasetSample(StrictBaseModel):
    sample_id: str
    dataset_sha256: str
    source: SourceReference


class DatasetAudit(StrictBaseModel):
    dataset_path: Path
    dataset_sha256: str
    raw_rows: int
    distinct_chunks: int
    query_rows: int
    blank_query_rows: int
    partial_query_rows: int
    distinct_query_ids: int
    repeated_query_excess_rows: int
    exact_duplicate_query_excess_rows: int
    exact_duplicate_query_groups: int
    conflicting_query_ids: int
    distinct_document_titles: int
    chunks_without_query: int
    chunks_with_query: int
    chunk_id_mismatches: int
    query_id_mismatches: int


class RetrievedContext(FlexibleBaseModel):
    text: str
    chunk_id: str
    scoped_chunk_id: str | None = None
    doc_title: str
    doc_hash: str | None = None
    prompt_position: int = Field(ge=0)
    synthetic_id: str | None = None
    synthetic_rank: int | None = Field(default=None, ge=0)
    context_rank: int | None = Field(default=None, ge=0)
    synthetic_score: float | None = None
    preprocessing_chunk_score: float | None = None


class RetrievedDemonstration(FlexibleBaseModel):
    synthetic_id: str
    synthetic_rank: int = Field(ge=0)
    synthetic_score: float | None = None
    reference_question: str
    reference_answer: str
    source_chunk_id: str | None = None
    source_doc_title: str | None = None
    contexts: list[RetrievedContext] = Field(default_factory=list)


def retrieved_contexts_from_demonstrations(
    demonstrations: list[RetrievedDemonstration],
) -> list[RetrievedContext]:
    contexts = [
        context for demonstration in demonstrations for context in demonstration.contexts
    ]
    return contexts_in_prompt_order(contexts)


def contexts_in_prompt_order(contexts: list[RetrievedContext]) -> list[RetrievedContext]:
    if contexts and all(context.prompt_position > 0 for context in contexts):
        return sorted(contexts, key=lambda context: context.prompt_position)
    return contexts


class RagAsapResponse(StrictBaseModel):
    status: Literal["ok", "warning", "error"]
    answer: str | None = None
    error: str | None = None
    demonstrations: list[RetrievedDemonstration] = Field(default_factory=list)

    @property
    def retrieved_contexts(self) -> list[RetrievedContext]:
        return retrieved_contexts_from_demonstrations(self.demonstrations)

    @field_validator("answer")
    @classmethod
    def normalize_blank_answer(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value


class InferenceStatus(str, Enum):
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"
    MALFORMED = "malformed"
    TIMEOUT = "timeout"


class InferenceRecord(StrictBaseModel):
    sample_id: str
    dataset_sha256: str
    source: SourceReference
    status: InferenceStatus
    answer: str | None = None
    error: str | None = None
    demonstrations: list[RetrievedDemonstration] = Field(default_factory=list)
    latency_seconds: float
    attempts: int = Field(ge=1)
    collected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def retrieved_contexts(self) -> list[RetrievedContext]:
        return retrieved_contexts_from_demonstrations(self.demonstrations)


class PreflightResult(StrictBaseModel):
    ping: Any
    config: dict[str, Any]
    tool_name: str
    return_contexts_supported: bool
