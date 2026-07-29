from __future__ import annotations

import csv
import json
import os
import tempfile
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from .config import sanitize_for_manifest

T = TypeVar("T", bound=BaseModel)


def create_run_dir(output_dir: str | Path, dataset_sha256: str, *, now: datetime | None = None) -> Path:
    timestamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    run_dir = Path(output_dir) / f"{timestamp}-{dataset_sha256[:12]}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def atomic_write_json(path: str | Path, payload: Any) -> None:
    text = json.dumps(
        _jsonable(payload),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    _atomic_write_text(Path(path), text + "\n")


def write_manifest(path: str | Path, payload: Any) -> None:
    atomic_write_json(path, sanitize_for_manifest(payload))


def read_jsonl(path: str | Path, model: type[T] | None = None) -> list[T] | list[dict[str, Any]]:
    jsonl_path = Path(path)
    if not jsonl_path.exists():
        return []
    records: list[Any] = []
    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                # A crash can leave a torn final line. Atomic checkpoint rewrites will repair it.
                continue
            records.append(model.model_validate(payload) if model is not None else payload)
    return records


def write_jsonl_atomic(path: str | Path, records: Iterable[Any]) -> None:
    text = "".join(
        json.dumps(_jsonable(record), ensure_ascii=False, sort_keys=True) + "\n"
        for record in records
    )
    _atomic_write_text(Path(path), text)


def append_jsonl_record_atomic(
    path: str | Path,
    record: Any,
    *,
    key_field: str = "sample_id",
    overwrite: bool = False,
) -> bool:
    """Append one JSONL record via atomic full-file replacement.

    Returns True when the checkpoint changed. Existing completed sample IDs are skipped unless
    overwrite=True, which replaces the existing record.
    """

    jsonl_path = Path(path)
    records = read_jsonl(jsonl_path)
    payload = _jsonable(record)
    key = payload[key_field]

    output: list[dict[str, Any]] = []
    changed = False
    seen = False
    for existing in records:
        if existing.get(key_field) == key:
            seen = True
            if overwrite:
                output.append(payload)
                changed = True
            else:
                output.append(existing)
        else:
            output.append(existing)
    if not seen:
        output.append(payload)
        changed = True

    if changed:
        write_jsonl_atomic(jsonl_path, output)
    return changed


def read_checkpoint_by_sample_id(path: str | Path) -> dict[str, dict[str, Any]]:
    records = read_jsonl(path)
    return {record["sample_id"]: record for record in records}


def atomic_write_csv(path: str | Path, rows: Iterable[dict[str, Any]], fieldnames: list[str]) -> None:
    csv_path = Path(path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", dir=csv_path.parent, delete=False
    ) as tmp:
        writer = csv.DictWriter(tmp, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        tmp_name = tmp.name
    os.replace(tmp_name, csv_path)


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    return value


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        tmp.write(text)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_name = tmp.name
    os.replace(tmp_name, path)
