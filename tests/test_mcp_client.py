from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.types import CallToolResult, TextContent

from asap_eval.artifacts import read_jsonl, write_jsonl_atomic
from asap_eval.config import InferenceConfig
from asap_eval.mcp_client import (
    MalformedMcpResponse,
    collect_samples,
    parse_rag_asap_response,
    preflight_mcp_client,
)
from asap_eval.models import DatasetSample, InferenceStatus, SourceReference


def make_sample(index: int) -> DatasetSample:
    return DatasetSample(
        sample_id=f"sample-{index}",
        dataset_sha256="d" * 64,
        source=SourceReference(
            source_row=index + 1,
            query_id=f"qid-{index}",
            query_text=f"Question {index}?",
            answer=f"Reference {index}",
            chunk_id=f"chunk-{index}",
            chunk_text=f"Chunk {index}",
            doc_title="Doc",
        ),
    )


def ok_payload(answer: str = "Answer") -> dict[str, Any]:
    return {
        "status": "ok",
        "answer": answer,
        "error": None,
        "retrieved_contexts": [
            {
                "text": "Context",
                "chunk_id": "chunk-1",
                "scoped_chunk_id": "doc:chunk-1",
                "doc_title": "Doc",
                "doc_hash": "hash",
                "prompt_position": 0,
                "synthetic_id": "syn-1",
                "synthetic_rank": 0,
                "context_rank": 0,
                "synthetic_score": 0.9,
                "preprocessing_chunk_score": None,
            }
        ],
        "demonstrations": [],
    }


def structured_tool_result(payload: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        content=[TextContent(type="text", text=payload.get("answer") or payload.get("error") or "")],
        data=None,
        structured_content=payload,
    )


class FakeClient:
    def __init__(self, responses: list[Any], *, delay: float = 0) -> None:
        self.responses = list(responses)
        self.delay = delay
        self.calls: list[dict[str, Any]] = []
        self.active = 0
        self.max_active = 0
        self.entered = False

    async def __aenter__(self) -> FakeClient:
        self.entered = True
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        assert tool_name == "RAG_ASAP"
        self.calls.append(arguments)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            response = self.responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response
        finally:
            self.active -= 1

    async def ping(self) -> dict[str, str]:
        return {"status": "ok"}

    async def get_config(self) -> dict[str, str]:
        return {"index_prefix": "test"}

    async def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "RAG_ASAP",
                "inputSchema": {
                    "properties": {
                        "user_query": {"type": "string"},
                        "return_contexts": {"type": "boolean"},
                    }
                },
            }
        ]


def test_parser_accepts_structured_content_envelope() -> None:
    parsed = parse_rag_asap_response({"structuredContent": ok_payload()}, contexts_requested=True)
    assert parsed.status == "ok"
    assert parsed.answer == "Answer"
    assert parsed.retrieved_contexts[0].chunk_id == "chunk-1"


def test_parser_unwraps_fastmcp_result_envelope() -> None:
    parsed = parse_rag_asap_response(
        {"structuredContent": {"result": ok_payload("Wrapped")}},
        contexts_requested=True,
    )
    assert parsed.status == "ok"
    assert parsed.answer == "Wrapped"


def test_parser_prefers_structured_content_over_text() -> None:
    parsed = parse_rag_asap_response(
        SimpleNamespace(
            content=[TextContent(type="text", text="Short text answer")],
            data=None,
            structured_content=ok_payload("Structured answer"),
        ),
        contexts_requested=True,
    )

    assert parsed.answer == "Structured answer"
    assert parsed.retrieved_contexts[0].text == "Context"


def test_parser_rejects_data_without_structured_content() -> None:
    with pytest.raises(MalformedMcpResponse, match="RagAsapResponse schema"):
        parse_rag_asap_response(
            SimpleNamespace(
                content=[TextContent(type="text", text="Short text answer")],
                data=ok_payload("Data answer"),
                structured_content=None,
            ),
            contexts_requested=True,
        )


def test_parser_accepts_raw_mcp_structured_content() -> None:
    parsed = parse_rag_asap_response(
        CallToolResult(
            content=[TextContent(type="text", text="Short text answer")],
            structuredContent=ok_payload("Raw structured answer"),
        ),
        contexts_requested=True,
    )

    assert parsed.answer == "Raw structured answer"
    assert parsed.retrieved_contexts[0].chunk_id == "chunk-1"


def test_parser_does_not_use_content_text_for_context_metrics() -> None:
    with pytest.raises(MalformedMcpResponse, match="RagAsapResponse schema"):
        parse_rag_asap_response(
            SimpleNamespace(
                content=[TextContent(type="text", text=json.dumps(ok_payload()))],
                data=None,
                structured_content=None,
            ),
            contexts_requested=True,
        )


def test_parser_rejects_plain_string_when_contexts_requested() -> None:
    with pytest.raises(MalformedMcpResponse, match="plain string"):
        parse_rag_asap_response("plain answer", contexts_requested=True)


@pytest.mark.asyncio
async def test_preflight_checks_tool_and_return_contexts() -> None:
    client = FakeClient([])
    result = await preflight_mcp_client(client, tool_name="RAG_ASAP")
    assert result.return_contexts_supported is True
    assert result.config["index_prefix"] == "test"


@pytest.mark.asyncio
async def test_fake_mcp_success_structured_error_and_malformed(tmp_path: Path) -> None:
    client = FakeClient(
        [
            structured_tool_result(ok_payload("A1")),
            structured_tool_result({"status": "error", "answer": None, "error": "component failed"}),
            "plain string",
        ]
    )
    records = await collect_samples(
        [make_sample(1), make_sample(2), make_sample(3)],
        client_factory=lambda: client,
        tool_name="RAG_ASAP",
        inference=InferenceConfig(max_concurrency=2, timeout_seconds=1, max_retries=0),
        checkpoint_path=str(tmp_path / "inference.jsonl"),
    )

    assert [record.status for record in records] == [
        InferenceStatus.OK,
        InferenceStatus.ERROR,
        InferenceStatus.MALFORMED,
    ]
    assert all("user_query" in call for call in client.calls)
    assert all("question" not in call for call in client.calls)
    assert records[0].answer == "A1"
    assert records[1].error == "component failed"
    assert records[2].error and "plain string" in records[2].error


@pytest.mark.asyncio
async def test_timeout_retry_and_concurrency_limit(tmp_path: Path) -> None:
    retry_client = FakeClient([RuntimeError("temporary"), structured_tool_result(ok_payload("retried"))])
    retry_records = await collect_samples(
        [make_sample(1)],
        client_factory=lambda: retry_client,
        tool_name="RAG_ASAP",
        inference=InferenceConfig(
            max_concurrency=1,
            timeout_seconds=1,
            max_retries=1,
            initial_backoff_seconds=0.001,
            max_backoff_seconds=0.001,
        ),
        checkpoint_path=str(tmp_path / "retry.jsonl"),
        sleep=lambda _: None,
    )
    assert retry_records[0].status == InferenceStatus.OK
    assert retry_records[0].attempts == 2

    timeout_client = FakeClient([structured_tool_result(ok_payload())], delay=0.05)
    timeout_records = await collect_samples(
        [make_sample(2)],
        client_factory=lambda: timeout_client,
        tool_name="RAG_ASAP",
        inference=InferenceConfig(max_concurrency=1, timeout_seconds=0.001, max_retries=1),
        checkpoint_path=str(tmp_path / "timeout.jsonl"),
        sleep=lambda _: None,
    )
    assert timeout_records[0].status == InferenceStatus.TIMEOUT
    assert timeout_records[0].attempts == 2

    slow_client = FakeClient(
        [structured_tool_result(ok_payload(str(i))) for i in range(5)], delay=0.01
    )
    concurrency_records = await collect_samples(
        [make_sample(i) for i in range(5)],
        client_factory=lambda: slow_client,
        tool_name="RAG_ASAP",
        inference=InferenceConfig(max_concurrency=2, timeout_seconds=1, max_retries=0),
        checkpoint_path=str(tmp_path / "concurrency.jsonl"),
    )
    assert len(concurrency_records) == 5
    assert slow_client.max_active <= 2


@pytest.mark.asyncio
async def test_resume_skips_completed_without_duplicate_jsonl(tmp_path: Path) -> None:
    checkpoint = tmp_path / "resume.jsonl"
    first = make_sample(1)
    existing = {
        "sample_id": first.sample_id,
        "dataset_sha256": first.dataset_sha256,
        "source": first.source.model_dump(mode="json"),
        "status": "ok",
        "answer": "already done",
        "error": None,
        "retrieved_contexts": [],
        "demonstrations": [],
        "latency_seconds": 0.1,
        "attempts": 1,
        "collected_at": "2026-07-20T00:00:00Z",
    }
    write_jsonl_atomic(checkpoint, [existing])

    client = FakeClient([structured_tool_result(ok_payload("new"))])
    records = await collect_samples(
        [first, make_sample(2)],
        client_factory=lambda: client,
        tool_name="RAG_ASAP",
        inference=InferenceConfig(max_concurrency=2, timeout_seconds=1, max_retries=0),
        checkpoint_path=str(checkpoint),
    )

    assert [record.answer for record in records] == ["already done", "new"]
    assert len(client.calls) == 1
    assert len(read_jsonl(checkpoint)) == 2


@pytest.mark.asyncio
async def test_partially_written_run_resumes_without_duplicating_jsonl(tmp_path: Path) -> None:
    checkpoint = tmp_path / "partial.jsonl"
    first = make_sample(1)
    complete = {
        "sample_id": first.sample_id,
        "dataset_sha256": first.dataset_sha256,
        "source": first.source.model_dump(mode="json"),
        "status": "ok",
        "answer": "already done",
        "error": None,
        "retrieved_contexts": [],
        "demonstrations": [],
        "latency_seconds": 0.1,
        "attempts": 1,
        "collected_at": "2026-07-20T00:00:00Z",
    }
    checkpoint.write_text(
        json.dumps(complete, ensure_ascii=False) + "\n" + '{"sample_id": "torn"',
        encoding="utf-8",
    )

    client = FakeClient([structured_tool_result(ok_payload("new"))])
    records = await collect_samples(
        [first, make_sample(2)],
        client_factory=lambda: client,
        tool_name="RAG_ASAP",
        inference=InferenceConfig(max_concurrency=2, timeout_seconds=1, max_retries=0),
        checkpoint_path=str(checkpoint),
    )

    assert [record.answer for record in records] == ["already done", "new"]
    assert len(read_jsonl(checkpoint)) == 2
    assert checkpoint.read_text(encoding="utf-8").count(first.sample_id) == 1
