from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

import pytest

from asap_eval.dataset import DatasetValidationError, load_dataset, sample_id_for

DATASET = Path(__file__).resolve().parents[1] / "data" / "squad_selected_full.csv"


def test_canonical_data_audit_matches_planning_counts() -> None:
    audit, samples = load_dataset(DATASET)

    assert audit.raw_rows == 10_000
    assert audit.distinct_chunks == 1_867
    assert audit.query_rows == 1_843
    assert audit.blank_query_rows == 8_157
    assert audit.partial_query_rows == 0
    assert audit.chunks_without_query == 786
    assert audit.chunks_with_query == 1_081
    assert audit.distinct_query_ids == 1_825
    assert audit.repeated_query_excess_rows == 18
    assert audit.exact_duplicate_query_excess_rows == 6
    assert audit.conflicting_query_ids == 7
    assert audit.distinct_document_titles == 30
    assert audit.chunk_id_mismatches == 0
    assert audit.query_id_mismatches == 0
    assert len(samples) == 1_843


def test_source_order_and_duplicate_query_rows_are_preserved() -> None:
    _, samples = load_dataset(DATASET)

    assert [sample.source.source_row for sample in samples] == sorted(
        sample.source.source_row for sample in samples
    )
    query_id_counts = Counter(sample.source.query_id for sample in samples)
    assert sum(count - 1 for count in query_id_counts.values() if count > 1) == 18

    exact_counts = Counter(
        (
            sample.source.query_id,
            sample.source.query_text,
            sample.source.answer,
            sample.source.chunk_id,
            sample.source.doc_title,
        )
        for sample in samples
    )
    assert sum(count - 1 for count in exact_counts.values() if count > 1) == 6


def test_sample_ids_are_stable_and_unique_per_source_row() -> None:
    audit, first_samples = load_dataset(DATASET)
    _, second_samples = load_dataset(DATASET)

    first_ids = [sample.sample_id for sample in first_samples]
    assert first_ids == [sample.sample_id for sample in second_samples]
    assert len(first_ids) == len(set(first_ids))
    for sample in first_samples[:10]:
        assert sample.sample_id == sample_id_for(audit.dataset_sha256, sample.source.source_row)


def test_partial_query_group_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "partial.csv"
    chunk_text = "A chunk"
    rows = [
        {
            "doc_title": "Doc",
            "chunk_text": chunk_text,
            "query_text": "Question?",
            "answer": "",
            "query_id": "",
            "chunk_id": __import__("hashlib").sha256(chunk_text.encode()).hexdigest(),
        }
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    with pytest.raises(DatasetValidationError, match="row 2: partial query group"):
        load_dataset(path)
