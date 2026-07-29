from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from asap_eval.artifacts import (
    append_jsonl_record_atomic,
    atomic_write_csv,
    atomic_write_json,
    create_run_dir,
    read_jsonl,
    write_manifest,
)
from asap_eval.config import EvalConfig, InferenceConfig


def test_config_redacts_credentials_from_dump_manifest_and_exception_repr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk-test-secret-value"
    monkeypatch.setenv("JUDGE_LLM_API_KEY", secret)
    monkeypatch.setenv("JUDGE_LLM_BASE_URL", "https://judge.example")
    monkeypatch.setenv("JUDGE_LLM_MODEL_NAME", "judge-model")
    monkeypatch.setenv("JUDGE_EMBED_API_KEY", "embed-secret-value")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
dataset_path = "data/squad_selected_full.csv"
output_dir = "results"
mcp_url = "http://localhost:8100/mcp/"
tool_name = "RAG_ASAP"

[inference]
max_concurrency = 4
timeout_seconds = 10
max_retries = 1

[ragas]
max_workers = 2
timeout_seconds = 20
max_retries = 1
max_wait_seconds = 5
seed = 42
batch_size = 8
raise_exceptions = false
""",
        encoding="utf-8",
    )

    config = EvalConfig.from_toml(config_path)
    dumped = json.dumps(config.sanitized_model_dump(), ensure_ascii=False)
    assert secret not in dumped
    assert "embed-secret-value" not in dumped
    assert "**********" in dumped

    manifest_path = tmp_path / "manifest.json"
    write_manifest(manifest_path, {"config": config})
    manifest = manifest_path.read_text(encoding="utf-8")
    assert secret not in manifest
    assert "embed-secret-value" not in manifest

    bad_config_path = tmp_path / "bad.toml"
    bad_config_path.write_text(config_path.read_text().replace("max_concurrency = 4", "max_concurrency = 0"))
    with pytest.raises(ValidationError) as excinfo:
        EvalConfig.from_toml(bad_config_path)
    assert secret not in repr(excinfo.value)
    assert "embed-secret-value" not in repr(excinfo.value)


def test_positive_inference_limits_are_validated() -> None:
    with pytest.raises(ValidationError):
        InferenceConfig(max_concurrency=0)
    with pytest.raises(ValidationError):
        InferenceConfig(timeout_seconds=0)
    with pytest.raises(ValidationError):
        InferenceConfig(initial_backoff_seconds=2, max_backoff_seconds=1)


def test_atomic_artifact_helpers(tmp_path: Path) -> None:
    run_dir = create_run_dir(tmp_path, "abcdef1234567890")
    assert run_dir.name.endswith("abcdef123456")

    json_path = run_dir / "summary.json"
    unicode_answer = "\N{CYRILLIC CAPITAL LETTER EN}\N{CYRILLIC SMALL LETTER A}"
    atomic_write_json(json_path, {"answer": unicode_answer, "ok": True})
    json_text = json_path.read_text(encoding="utf-8")
    assert json.loads(json_text) == {"answer": unicode_answer, "ok": True}
    assert unicode_answer in json_text
    assert "\\u041d" not in json_text

    csv_path = run_dir / "scores.csv"
    atomic_write_csv(csv_path, [{"sample_id": "a", "score": 1}], ["sample_id", "score"])
    assert csv_path.read_text(encoding="utf-8").splitlines()[0] == "sample_id,score"

    jsonl_path = run_dir / "records.jsonl"
    assert append_jsonl_record_atomic(jsonl_path, {"sample_id": "a", "value": 1})
    assert not append_jsonl_record_atomic(jsonl_path, {"sample_id": "a", "value": 2})
    assert append_jsonl_record_atomic(
        jsonl_path,
        {"sample_id": "a", "answer": unicode_answer, "value": 2},
        overwrite=True,
    )
    jsonl_text = jsonl_path.read_text(encoding="utf-8")
    assert read_jsonl(jsonl_path) == [{"sample_id": "a", "answer": unicode_answer, "value": 2}]
    assert unicode_answer in jsonl_text
    assert "\\u041d" not in jsonl_text
