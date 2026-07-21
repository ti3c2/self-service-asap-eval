from __future__ import annotations

import csv
import hashlib
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path

from .models import DatasetAudit, DatasetSample, SourceReference

QUERY_COLUMNS = ("query_id", "query_text", "answer")
DOCUMENT_COLUMNS = ("chunk_id", "chunk_text")
DOC_TITLE_COLUMN = "doc_title"
LEGACY_DOC_TITLE_COLUMN = "title"


class DatasetValidationError(ValueError):
    pass


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str:
    hasher = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def sample_id_for(dataset_sha256: str, source_row: int) -> str:
    return f"{dataset_sha256[:12]}-{source_row:06d}"


def load_dataset(path: str | Path) -> tuple[DatasetAudit, list[DatasetSample]]:
    rows = _read_rows(path)
    return _audit_and_samples(Path(path), rows)


def audit_dataset(path: str | Path) -> DatasetAudit:
    audit, _ = load_dataset(path)
    return audit


def _read_rows(path: str | Path) -> list[tuple[int, dict[str, str]]]:
    csv_path = Path(path)
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise DatasetValidationError(f"{csv_path}: missing CSV header")
        _validate_columns(csv_path, reader.fieldnames)
        return [(row_number, _normalize_row(row)) for row_number, row in enumerate(reader, start=2)]


def _validate_columns(path: Path, fieldnames: Iterable[str]) -> None:
    columns = set(fieldnames)
    missing = [column for column in (*DOCUMENT_COLUMNS, *QUERY_COLUMNS) if column not in columns]
    if missing:
        raise DatasetValidationError(f"{path}: missing required CSV columns: {', '.join(missing)}")
    if DOC_TITLE_COLUMN not in columns and LEGACY_DOC_TITLE_COLUMN not in columns:
        raise DatasetValidationError(
            f"{path}: missing required CSV column: {DOC_TITLE_COLUMN} "
            f"(legacy alias {LEGACY_DOC_TITLE_COLUMN!r} is accepted)"
        )


def _normalize_row(row: dict[str, str | None]) -> dict[str, str]:
    normalized = {key: (value or "") for key, value in row.items()}
    if DOC_TITLE_COLUMN not in normalized:
        normalized[DOC_TITLE_COLUMN] = normalized.get(LEGACY_DOC_TITLE_COLUMN, "")
    return normalized


def _audit_and_samples(
    path: Path, rows: list[tuple[int, dict[str, str]]]
) -> tuple[DatasetAudit, list[DatasetSample]]:
    dataset_hash = sha256_file(path)
    chunk_identity: dict[str, tuple[str, str]] = {}
    chunk_rows_with_query: set[str] = set()
    query_rows: list[tuple[int, dict[str, str]]] = []
    partial_query_rows = 0
    chunk_id_mismatches = 0
    query_id_mismatches = 0

    for row_number, row in rows:
        _require_document_fields(path, row_number, row)
        chunk_id = row["chunk_id"]
        chunk_text = row["chunk_text"]
        doc_title = row[DOC_TITLE_COLUMN]

        if sha256_text(chunk_text) != chunk_id:
            chunk_id_mismatches += 1

        existing_chunk = chunk_identity.get(chunk_id)
        if existing_chunk is None:
            chunk_identity[chunk_id] = (chunk_text, doc_title)
        elif existing_chunk != (chunk_text, doc_title):
            raise DatasetValidationError(
                f"{path}: row {row_number}: chunk_id {chunk_id} maps to conflicting "
                "chunk_text/doc_title values"
            )

        query_presence = [bool(row[column].strip()) for column in QUERY_COLUMNS]
        if all(query_presence):
            if sha256_text(row["query_text"]) != row["query_id"]:
                query_id_mismatches += 1
            query_rows.append((row_number, row))
            chunk_rows_with_query.add(chunk_id)
        elif any(query_presence):
            partial_query_rows += 1
            missing = [column for column in QUERY_COLUMNS if not row[column]]
            present = [column for column in QUERY_COLUMNS if row[column]]
            raise DatasetValidationError(
                f"{path}: row {row_number}: partial query group; "
                f"present={present}, missing={missing}"
            )

    query_ids = [row["query_id"] for _, row in query_rows]
    query_id_counts = Counter(query_ids)
    exact_duplicate_counts = Counter(
        (
            row["query_id"],
            row["query_text"],
            row["answer"],
            row["chunk_id"],
            row[DOC_TITLE_COLUMN],
        )
        for _, row in query_rows
    )
    references_by_query_id: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
    for _, row in query_rows:
        references_by_query_id[row["query_id"]].add(
            (row["answer"], row["chunk_id"], row[DOC_TITLE_COLUMN])
        )

    samples = [
        DatasetSample(
            sample_id=sample_id_for(dataset_hash, row_number),
            dataset_sha256=dataset_hash,
            source=SourceReference(
                source_row=row_number,
                query_id=row["query_id"],
                query_text=row["query_text"],
                answer=row["answer"],
                chunk_id=row["chunk_id"],
                chunk_text=row["chunk_text"],
                doc_title=row[DOC_TITLE_COLUMN],
            ),
        )
        for row_number, row in query_rows
    ]

    audit = DatasetAudit(
        dataset_path=path,
        dataset_sha256=dataset_hash,
        raw_rows=len(rows),
        distinct_chunks=len(chunk_identity),
        query_rows=len(query_rows),
        blank_query_rows=len(rows) - len(query_rows),
        partial_query_rows=partial_query_rows,
        distinct_query_ids=len(query_id_counts),
        repeated_query_excess_rows=sum(count - 1 for count in query_id_counts.values() if count > 1),
        exact_duplicate_query_excess_rows=sum(
            count - 1 for count in exact_duplicate_counts.values() if count > 1
        ),
        exact_duplicate_query_groups=sum(1 for count in exact_duplicate_counts.values() if count > 1),
        conflicting_query_ids=sum(1 for refs in references_by_query_id.values() if len(refs) > 1),
        distinct_document_titles=len({title for _, title in chunk_identity.values()}),
        chunks_without_query=len(set(chunk_identity) - chunk_rows_with_query),
        chunks_with_query=len(chunk_rows_with_query),
        chunk_id_mismatches=chunk_id_mismatches,
        query_id_mismatches=query_id_mismatches,
    )
    return audit, samples


def _require_document_fields(path: Path, row_number: int, row: dict[str, str]) -> None:
    missing = [column for column in (*DOCUMENT_COLUMNS, DOC_TITLE_COLUMN) if not row[column].strip()]
    if missing:
        raise DatasetValidationError(
            f"{path}: row {row_number}: missing required document fields: {', '.join(missing)}"
        )
