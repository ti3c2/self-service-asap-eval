from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:  # pragma: no cover - Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

SECRET_FIELD_MARKER = "**********"


class SafeBaseModel(BaseModel):
    """Base model with strict fields and a recursive sanitized dump."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    def sanitized_model_dump(self) -> dict[str, Any]:
        return sanitize_for_manifest(self.model_dump(mode="python"))


class InferenceConfig(SafeBaseModel):
    max_concurrency: int = Field(default=8, gt=0)
    timeout_seconds: float = Field(default=120, gt=0)
    max_retries: int = Field(default=3, ge=0)
    initial_backoff_seconds: float = Field(default=0.25, gt=0)
    max_backoff_seconds: float = Field(default=5, gt=0)

    @model_validator(mode="after")
    def validate_backoff_order(self) -> InferenceConfig:
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError("max_backoff_seconds must be >= initial_backoff_seconds")
        return self


class RagasConfig(SafeBaseModel):
    max_workers: int = Field(default=8, gt=0)
    timeout_seconds: float = Field(default=180, gt=0)
    max_retries: int = Field(default=3, ge=0)
    max_wait_seconds: float = Field(default=60, gt=0)
    seed: int = 42
    batch_size: int = Field(default=32, gt=0)
    raise_exceptions: bool = False


class JudgeEnvironment(SafeBaseModel):
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None
    llm_model_name: str | None = None
    embed_api_key: SecretStr | None = None
    embed_base_url: str | None = None
    embed_model_name: str | None = None


class EvalConfig(SafeBaseModel):
    dataset_path: Path
    output_dir: Path = Path("results")
    mcp_url: str = "http://localhost:8100/mcp/"
    tool_name: str = "RAG_ASAP"
    inference: InferenceConfig = Field(default_factory=InferenceConfig)
    ragas: RagasConfig = Field(default_factory=RagasConfig)
    judge: JudgeEnvironment = Field(default_factory=JudgeEnvironment)

    @classmethod
    def from_toml(
        cls,
        path: str | Path,
        *,
        env_file: str | Path | None = None,
        load_environment: bool = True,
    ) -> EvalConfig:
        config_path = Path(path)
        if env_file is not None:
            load_dotenv(env_file)
        elif load_environment:
            load_dotenv()

        with config_path.open("rb") as f:
            raw = tomllib.load(f)
        raw["judge"] = load_judge_environment()
        return cls.model_validate(raw)


def load_judge_environment(environ: dict[str, str] | None = None) -> JudgeEnvironment:
    env = os.environ if environ is None else environ
    return JudgeEnvironment(
        llm_api_key=env.get("JUDGE_LLM_API_KEY"),
        llm_base_url=env.get("JUDGE_LLM_BASE_URL"),
        llm_model_name=env.get("JUDGE_LLM_MODEL_NAME"),
        embed_api_key=env.get("JUDGE_EMBED_API_KEY"),
        embed_base_url=env.get("JUDGE_EMBED_BASE_URL"),
        embed_model_name=env.get("JUDGE_EMBED_MODEL_NAME"),
    )


def sanitize_for_manifest(value: Any) -> Any:
    """Remove secret-bearing values from objects before writing artifacts."""

    if isinstance(value, SecretStr):
        return SECRET_FIELD_MARKER if value.get_secret_value() else None
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, BaseModel):
        return sanitize_for_manifest(value.model_dump(mode="python"))
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            lower_key = str(key).lower()
            if any(token in lower_key for token in ("api_key", "authorization", "secret", "token")):
                sanitized[str(key)] = SECRET_FIELD_MARKER if item else None
            else:
                sanitized[str(key)] = sanitize_for_manifest(item)
        return sanitized
    if isinstance(value, list | tuple):
        return [sanitize_for_manifest(item) for item in value]
    return value
