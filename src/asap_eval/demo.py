from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import RagAsapResponse


@dataclass(frozen=True)
class DemoQuery:
    query_id: str
    question: str


def load_demo_queries(path: Path) -> list[DemoQuery]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries = raw.get("queries") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        raise ValueError(f"Demo query file must contain a list or a 'queries' list: {path}")

    queries: list[DemoQuery] = []
    for index, entry in enumerate(entries, start=1):
        queries.append(_demo_query_from_entry(entry, index=index))
    return queries


def demo_queries_from_cli(questions: list[str]) -> list[DemoQuery]:
    queries: list[DemoQuery] = []
    for index, question in enumerate(questions, start=1):
        stripped = question.strip()
        if not stripped:
            raise ValueError(f"CLI question #{index} is empty.")
        queries.append(DemoQuery(query_id=f"cli-{index}", question=stripped))
    return queries


def render_demo_response(
    *,
    index: int,
    total: int,
    query: DemoQuery,
    response: RagAsapResponse,
    latency_seconds: float,
    max_contexts: int,
    max_demonstrations: int,
    snippet_chars: int,
) -> str:
    max_contexts = max(0, max_contexts)
    max_demonstrations = max(0, max_demonstrations)

    lines = [
        "=" * 88,
        f"[{index}/{total}] {query.query_id}",
        f"Question: {query.question}",
        (
            f"Status: {response.status} | latency: {latency_seconds:.2f}s | "
            f"contexts: {len(response.retrieved_contexts)} | "
            f"demonstrations: {len(response.demonstrations)}"
        ),
    ]
    if response.error:
        lines.append(f"Error: {response.error}")

    lines.extend(["", "Answer:"])
    lines.extend(_indent_block(response.answer or "<no answer>"))
    lines.extend(["", f"Retrieval contexts ({len(response.retrieved_contexts)} total):"])
    lines.extend(
        _render_contexts(
            response.retrieved_contexts,
            max_items=max_contexts,
            snippet_chars=snippet_chars,
        )
    )

    lines.extend(["", f"Demonstrations ({len(response.demonstrations)} total):"])
    lines.extend(
        _render_demonstrations(
            response.demonstrations,
            max_items=max_demonstrations,
            snippet_chars=snippet_chars,
        )
    )
    return "\n".join(lines)


def render_demo_error(
    *,
    index: int,
    total: int,
    query: DemoQuery,
    latency_seconds: float,
    error: BaseException,
) -> str:
    return "\n".join(
        [
            "=" * 88,
            f"[{index}/{total}] {query.query_id}",
            f"Question: {query.question}",
            f"Status: call_failed | latency: {latency_seconds:.2f}s",
            f"Error: {type(error).__name__}: {error}",
        ]
    )


def _demo_query_from_entry(entry: Any, *, index: int) -> DemoQuery:
    if isinstance(entry, str):
        question = entry.strip()
        query_id = f"query-{index}"
    elif isinstance(entry, dict):
        raw_question = entry.get("question", entry.get("query"))
        raw_query_id = entry.get("id", entry.get("name", f"query-{index}"))
        if not isinstance(raw_question, str):
            raise ValueError(f"Demo query #{index} must have a string question/query field.")
        if not isinstance(raw_query_id, str):
            raise ValueError(f"Demo query #{index} must have a string id/name field.")
        question = raw_question.strip()
        query_id = raw_query_id.strip() or f"query-{index}"
    else:
        raise ValueError(f"Demo query #{index} must be an object or string.")

    if not question:
        raise ValueError(f"Demo query #{index} is empty.")
    return DemoQuery(query_id=query_id, question=question)


def _render_contexts(
    contexts: list[Any],
    *,
    max_items: int,
    snippet_chars: int,
) -> list[str]:
    if not contexts:
        return ["  <none>"]

    lines: list[str] = []
    for display_index, context in enumerate(contexts[:max_items], start=1):
        score_text = _score_text(
            context,
            [
                ("synthetic_score", "synthetic_score"),
                ("chunk_score", "preprocessing_chunk_score"),
            ],
        )
        lines.append(
            f"  {display_index}. prompt_position={_value(context, 'prompt_position')} "
            f"context_rank={_value(context, 'context_rank')} "
            f"synthetic_rank={_value(context, 'synthetic_rank')} "
            f"chunk_id={_value(context, 'chunk_id')}"
        )
        lines.append(f"     doc_title={_value(context, 'doc_title')}{score_text}")
        lines.append(f"     text={_snippet(str(_value(context, 'text') or ''), snippet_chars)}")

    omitted = len(contexts) - max_items
    if omitted > 0:
        lines.append(f"  ... {omitted} more context(s) omitted")
    return lines


def _render_demonstrations(
    demonstrations: list[Any],
    *,
    max_items: int,
    snippet_chars: int,
) -> list[str]:
    if not demonstrations:
        return ["  <none>"]

    lines: list[str] = []
    for display_index, demonstration in enumerate(demonstrations[:max_items], start=1):
        score_text = _score_text(demonstration, [("synthetic_score", "synthetic_score")])
        lines.append(
            f"  {display_index}. synthetic_rank={_value(demonstration, 'synthetic_rank')} "
            f"synthetic_id={_value(demonstration, 'synthetic_id')}{score_text}"
        )
        lines.append(
            f"     source_doc_title={_value(demonstration, 'source_doc_title')} "
            f"source_chunk_id={_value(demonstration, 'source_chunk_id')}"
        )
        lines.append(
            "     synthetic_reference_question="
            f"{_snippet(str(_value(demonstration, 'reference_question') or ''), snippet_chars)}"
        )
        lines.append(
            "     synthetic_reference_answer="
            f"{_snippet(str(_value(demonstration, 'reference_answer') or ''), snippet_chars)}"
        )

    omitted = len(demonstrations) - max_items
    if omitted > 0:
        lines.append(f"  ... {omitted} more demonstration(s) omitted")
    return lines


def _indent_block(text: str) -> list[str]:
    lines = text.splitlines() or [""]
    return [f"  {line}" for line in lines]


def _snippet(text: str, max_chars: int) -> str:
    collapsed = re.sub(r"\s+", " ", text).strip()
    if max_chars <= 0:
        return ""
    if max_chars <= 3:
        return collapsed[:max_chars]
    if len(collapsed) <= max_chars:
        return collapsed
    return f"{collapsed[: max_chars - 3].rstrip()}..."


def _score_text(value: Any, pairs: list[tuple[str, str]]) -> str:
    parts = []
    for label, field in pairs:
        score = _value(value, field)
        if score is None:
            continue
        if isinstance(score, float):
            parts.append(f"{label}={score:.4g}")
        else:
            parts.append(f"{label}={score}")
    return f" | {' | '.join(parts)}" if parts else ""


def _value(value: Any, field: str) -> Any:
    if isinstance(value, dict):
        return value.get(field)
    return getattr(value, field, None)
