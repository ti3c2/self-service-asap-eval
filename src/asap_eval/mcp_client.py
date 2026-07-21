from __future__ import annotations

import asyncio
import inspect
import random
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from .artifacts import append_jsonl_record_atomic, read_checkpoint_by_sample_id
from .config import InferenceConfig
from .models import (
    DatasetSample,
    InferenceRecord,
    InferenceStatus,
    PreflightResult,
    RagAsapResponse,
)


class MalformedMcpResponse(ValueError):
    pass


class McpPreflightError(RuntimeError):
    pass


class ManagedMcpClient(Protocol):
    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any: ...


ClientFactory = Callable[[], Any]
SleepFunc = Callable[[float], Any]


@dataclass(frozen=True)
class ToolCallShape:
    question_parameter: str = "user_query"
    contexts_parameter: str = "return_contexts"


DEFAULT_TOOL_CALL_SHAPE = ToolCallShape()


def parse_rag_asap_response(payload: Any, *, contexts_requested: bool = True) -> RagAsapResponse:
    """Parse the structured response Agent B exposes through FastMCP.

    The parser accepts Pydantic model instances and plain dictionaries. It unwraps common MCP
    result envelopes (`data`, `structuredContent`) but rejects a plain string whenever the
    collector requested contexts, because judging an error/string fallback as an answer would
    corrupt the benchmark.
    """

    candidate = _unwrap_mcp_payload(payload)
    if isinstance(candidate, str):
        if contexts_requested:
            raise MalformedMcpResponse("received plain string although return_contexts=True")
        return RagAsapResponse(status="ok", answer=candidate)

    if isinstance(candidate, BaseModel):
        candidate = candidate.model_dump(mode="python")
    elif not isinstance(candidate, dict):
        if hasattr(candidate, "model_dump"):
            candidate = candidate.model_dump(mode="python")
        elif hasattr(candidate, "__dict__"):
            candidate = vars(candidate)
        else:
            raise MalformedMcpResponse(f"unsupported MCP response type: {type(candidate).__name__}")

    try:
        return RagAsapResponse.model_validate(candidate)
    except ValidationError as exc:
        raise MalformedMcpResponse("response does not match RagAsapResponse schema") from exc


async def preflight_mcp_client(
    client: Any,
    *,
    tool_name: str,
    return_contexts_parameter: str = "return_contexts",
) -> PreflightResult:
    """Run evaluator-side readiness/schema checks against an MCP session."""

    ping = await _maybe_await(client.ping())
    config = await _maybe_await(client.get_config())
    tools = await _maybe_await(client.list_tools())
    tool = _find_tool(tools, tool_name)
    if tool is None:
        raise McpPreflightError(f"MCP tool {tool_name!r} was not found")
    if not _tool_has_parameter(tool, return_contexts_parameter):
        raise McpPreflightError(
            f"MCP tool {tool_name!r} does not expose {return_contexts_parameter!r}"
        )
    return PreflightResult(
        ping=ping,
        config=dict(config or {}),
        tool_name=tool_name,
        return_contexts_supported=True,
    )


async def collect_samples(
    samples: list[DatasetSample],
    *,
    client_factory: ClientFactory,
    tool_name: str,
    inference: InferenceConfig,
    checkpoint_path: str,
    overwrite: bool = False,
    call_shape: ToolCallShape = DEFAULT_TOOL_CALL_SHAPE,
    sleep: SleepFunc = asyncio.sleep,
) -> list[InferenceRecord]:
    """Collect answers with one managed client session and order-preserving results."""

    completed = {} if overwrite else read_checkpoint_by_sample_id(checkpoint_path)
    results: list[InferenceRecord | None] = [None] * len(samples)
    to_issue: list[tuple[int, DatasetSample]] = []

    for index, sample in enumerate(samples):
        existing = completed.get(sample.sample_id)
        if existing is not None:
            results[index] = InferenceRecord.model_validate(existing)
        else:
            to_issue.append((index, sample))

    semaphore = asyncio.Semaphore(inference.max_concurrency)
    checkpoint_lock = asyncio.Lock()

    async with _managed_client(client_factory) as client:
        async def worker(index: int, sample: DatasetSample) -> None:
            async with semaphore:
                record = await _collect_one(
                    client,
                    sample,
                    tool_name=tool_name,
                    inference=inference,
                    call_shape=call_shape,
                    sleep=sleep,
                )
                async with checkpoint_lock:
                    append_jsonl_record_atomic(
                        checkpoint_path,
                        record,
                        key_field="sample_id",
                        overwrite=overwrite,
                    )
                results[index] = record

        await asyncio.gather(*(worker(index, sample) for index, sample in to_issue))

    return [record for record in results if record is not None]


async def _collect_one(
    client: ManagedMcpClient,
    sample: DatasetSample,
    *,
    tool_name: str,
    inference: InferenceConfig,
    call_shape: ToolCallShape,
    sleep: SleepFunc,
) -> InferenceRecord:
    started = time.monotonic()
    attempts_allowed = inference.max_retries + 1
    last_error: BaseException | None = None

    for attempt in range(1, attempts_allowed + 1):
        try:
            response = await asyncio.wait_for(
                client.call_tool(tool_name, _tool_arguments(sample, call_shape)),
                timeout=inference.timeout_seconds,
            )
            parsed = parse_rag_asap_response(response, contexts_requested=True)
            return _record_from_response(
                sample,
                parsed,
                attempts=attempt,
                latency_seconds=time.monotonic() - started,
            )
        except (TimeoutError, asyncio.TimeoutError) as exc:
            last_error = exc
            if attempt == attempts_allowed:
                return _error_record(
                    sample,
                    status=InferenceStatus.TIMEOUT,
                    error=f"timed out after {attempt} attempt(s)",
                    attempts=attempt,
                    latency_seconds=time.monotonic() - started,
                )
        except MalformedMcpResponse as exc:
            return _error_record(
                sample,
                status=InferenceStatus.MALFORMED,
                error=str(exc),
                attempts=attempt,
                latency_seconds=time.monotonic() - started,
            )
        except Exception as exc:  # transient transport/client failures
            last_error = exc
            if attempt == attempts_allowed:
                return _error_record(
                    sample,
                    status=InferenceStatus.ERROR,
                    error=f"{type(exc).__name__}: {exc}",
                    attempts=attempt,
                    latency_seconds=time.monotonic() - started,
                )

        delay = min(
            inference.max_backoff_seconds,
            inference.initial_backoff_seconds * (2 ** max(0, attempt - 1)),
        )
        await _maybe_await(sleep(random.uniform(0, delay)))

    return _error_record(
        sample,
        status=InferenceStatus.ERROR,
        error=f"collection failed: {last_error!r}",
        attempts=attempts_allowed,
        latency_seconds=time.monotonic() - started,
    )


def _record_from_response(
    sample: DatasetSample,
    response: RagAsapResponse,
    *,
    attempts: int,
    latency_seconds: float,
) -> InferenceRecord:
    status = InferenceStatus(response.status)
    return InferenceRecord(
        sample_id=sample.sample_id,
        dataset_sha256=sample.dataset_sha256,
        source=sample.source,
        status=status,
        answer=response.answer,
        error=response.error,
        retrieved_contexts=response.retrieved_contexts,
        demonstrations=response.demonstrations,
        latency_seconds=latency_seconds,
        attempts=attempts,
    )


def _error_record(
    sample: DatasetSample,
    *,
    status: InferenceStatus,
    error: str,
    attempts: int,
    latency_seconds: float,
) -> InferenceRecord:
    return InferenceRecord(
        sample_id=sample.sample_id,
        dataset_sha256=sample.dataset_sha256,
        source=sample.source,
        status=status,
        error=error,
        latency_seconds=latency_seconds,
        attempts=attempts,
    )


def _tool_arguments(sample: DatasetSample, call_shape: ToolCallShape) -> dict[str, Any]:
    return {
        call_shape.question_parameter: sample.source.query_text,
        call_shape.contexts_parameter: True,
    }


def _unwrap_mcp_payload(payload: Any) -> Any:
    candidate = payload
    for _ in range(3):
        if isinstance(candidate, BaseModel):
            return candidate
        if hasattr(candidate, "data"):
            candidate = candidate.data
            continue
        if isinstance(candidate, dict):
            for key in ("data", "structuredContent", "structured_content"):
                if key in candidate:
                    candidate = candidate[key]
                    break
            else:
                return candidate
            continue
        return candidate
    return candidate


def _find_tool(tools: Any, tool_name: str) -> Any | None:
    for tool in tools or []:
        name = _get_field(tool, "name")
        if name == tool_name:
            return tool
    return None


def _tool_has_parameter(tool: Any, parameter: str) -> bool:
    schema = (
        _get_field(tool, "inputSchema")
        or _get_field(tool, "input_schema")
        or _get_field(tool, "parameters")
        or _get_field(tool, "args_schema")
        or {}
    )
    if isinstance(schema, BaseModel):
        schema = schema.model_dump(mode="python")
    if hasattr(schema, "model_dump"):
        schema = schema.model_dump(mode="python")
    if not isinstance(schema, dict):
        return False
    properties = schema.get("properties") or schema.get("params") or {}
    return parameter in properties


def _get_field(value: Any, field: str) -> Any:
    if isinstance(value, dict):
        return value.get(field)
    return getattr(value, field, None)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


@asynccontextmanager
async def _managed_client(factory: ClientFactory) -> AsyncIterator[Any]:
    client = factory()
    client = await _maybe_await(client)
    if hasattr(client, "__aenter__"):
        async with client as managed:
            yield managed
    else:
        yield client
