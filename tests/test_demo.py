from __future__ import annotations

import json

import pytest

from asap_eval.demo import (
    DemoQuery,
    demo_queries_from_cli,
    load_demo_queries,
    render_demo_response,
)
from asap_eval.models import RagAsapResponse


def test_load_demo_queries_accepts_query_objects(tmp_path):
    query_file = tmp_path / "queries.json"
    query_file.write_text(
        json.dumps(
            {
                "queries": [
                    {"id": "known", "question": "What is known?"},
                    {"name": "alias", "query": "What is aliased?"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    queries = load_demo_queries(query_file)

    assert queries == [
        DemoQuery(query_id="known", question="What is known?"),
        DemoQuery(query_id="alias", question="What is aliased?"),
    ]


def test_demo_queries_from_cli_rejects_empty_questions():
    with pytest.raises(ValueError, match="empty"):
        demo_queries_from_cli(["   "])


def test_render_demo_response_prints_answer_contexts_and_demonstrations():
    response = RagAsapResponse.model_validate(
        {
            "status": "ok",
            "answer": "The answer.",
            "demonstrations": [
                {
                    "synthetic_id": "syn-1",
                    "synthetic_rank": 1,
                    "synthetic_score": 0.9,
                    "reference_question": "Reference question?",
                    "reference_answer": "Reference answer.",
                    "source_chunk_id": "chunk-1",
                    "source_doc_title": "Doc",
                    "contexts": [
                        {
                            "text": "Context text with enough detail to show truncation behavior.",
                            "chunk_id": "chunk-1",
                            "scoped_chunk_id": "doc:chunk-1",
                            "doc_title": "Doc",
                            "doc_hash": "hash",
                            "prompt_position": 1,
                            "synthetic_id": "syn-1",
                            "synthetic_rank": 1,
                            "context_rank": 1,
                            "synthetic_score": 0.9,
                        }
                    ],
                }
            ],
        }
    )

    output = render_demo_response(
        index=1,
        total=1,
        query=DemoQuery(query_id="q1", question="Question?"),
        response=response,
        latency_seconds=0.123,
        max_contexts=1,
        max_demonstrations=1,
        snippet_chars=20,
    )

    assert "Question: Question?" in output
    assert "Answer:\n  The answer." in output
    assert "Retrieval contexts (1 total):" in output
    assert "chunk_id=chunk-1" in output
    assert "Demonstrations (1 total):" in output
    assert "synthetic_reference_question=Reference question?" in output
    assert "synthetic_reference_answer=Reference answer." in output
