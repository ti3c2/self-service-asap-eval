from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import ragas
from pydantic import BaseModel

from .artifacts import create_run_dir, read_jsonl, write_manifest
from .config import EvalConfig
from .dataset import load_dataset
from .demo import (
    demo_queries_from_cli,
    load_demo_queries,
    render_demo_error,
    render_demo_response,
)
from .mcp_client import collect_samples, parse_rag_asap_response
from .metrics import RAGAS_METRIC_NAMES
from .models import InferenceRecord, PreflightResult
from .ragas_runner import run_ragas_evaluation
from .reporting import write_evaluation_artifacts


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "audit":
        command_audit(args)
    elif args.command == "collect":
        asyncio.run(command_collect(args))
    elif args.command == "evaluate":
        command_evaluate(args)
    elif args.command == "demo":
        asyncio.run(command_demo(args))
    elif args.command == "run":
        asyncio.run(command_run(args))
    else:  # pragma: no cover - argparse enforces a command
        parser.print_help()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="asap-eval")
    parser.add_argument("--version", action="version", version="asap-eval 0.1.0")
    subparsers = parser.add_subparsers(dest="command", required=True)

    audit = subparsers.add_parser("audit", help="Audit the configured dataset")
    _add_config_arg(audit)
    _add_dataset_path_arg(audit)

    collect = subparsers.add_parser("collect", help="Collect structured MCP answers")
    _add_config_arg(collect)
    _add_dataset_path_arg(collect)
    collect.add_argument("--max-samples", type=int, default=None)
    collect.add_argument("--output-dir", type=Path, default=None)
    collect.add_argument("--overwrite", action="store_true")

    evaluate_cmd = subparsers.add_parser("evaluate", help="Run RAGAS on collected answers")
    _add_config_arg(evaluate_cmd)
    _add_dataset_path_arg(evaluate_cmd)
    evaluate_cmd.add_argument("--run-dir", type=Path, required=True)
    evaluate_cmd.add_argument("--max-workers", type=int, default=None)

    demo = subparsers.add_parser("demo", help="Print live answers and retrieval traces")
    _add_config_arg(demo)
    demo.add_argument("--queries", type=Path, default=Path("data/rag_tool_demo_queries.json"))
    demo.add_argument(
        "--question",
        action="append",
        default=None,
        help="Ask an ad hoc question. Can be passed multiple times.",
    )
    demo.add_argument("--limit", type=int, default=None)
    demo.add_argument("--max-contexts", type=int, default=6)
    demo.add_argument("--max-demonstrations", type=int, default=4)
    demo.add_argument("--snippet-chars", type=int, default=700)
    demo.add_argument("--no-preflight", action="store_true")

    run_cmd = subparsers.add_parser("run", help="Collect answers, then run RAGAS")
    _add_config_arg(run_cmd)
    _add_dataset_path_arg(run_cmd)
    run_cmd.add_argument("--max-samples", type=int, default=None)
    run_cmd.add_argument("--output-dir", type=Path, default=None)
    run_cmd.add_argument("--overwrite", action="store_true")
    run_cmd.add_argument("--max-workers", type=int, default=None)
    return parser


def command_audit(args: argparse.Namespace) -> None:
    config = _load_config(args)
    audit, _samples = load_dataset(config.dataset_path)
    print(json.dumps(audit.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True))


async def command_collect(args: argparse.Namespace) -> Path:
    config = _load_config(args)
    audit, samples = load_dataset(config.dataset_path)
    samples = _select_samples(samples, args.max_samples)
    output_dir = args.output_dir or config.output_dir
    run_dir = create_run_dir(output_dir, audit.dataset_sha256)
    preflight = await live_preflight(config)
    manifest = build_run_manifest(
        config=config,
        audit=audit,
        selected_sample_count=len(samples),
        run_dir=run_dir,
        mcp_preflight=preflight,
    )
    write_manifest(run_dir / "run_manifest.json", manifest)

    records = await collect_samples(
        samples,
        client_factory=_client_factory(config.mcp_url),
        tool_name=config.tool_name,
        inference=config.inference,
        checkpoint_path=str(run_dir / "inference_samples.jsonl"),
        overwrite=args.overwrite,
    )
    print(f"Collected {len(records)} samples into {run_dir}")
    return run_dir


def command_evaluate(args: argparse.Namespace) -> None:
    config = _load_config(args)
    if args.max_workers is not None:
        config = config.model_copy(
            update={
                "ragas": config.ragas.model_copy(
                    update={"max_workers": args.max_workers}
                )
            }
        )
    run_dir = Path(args.run_dir)
    evaluate_run_dir(config, run_dir)
    print(f"Wrote evaluation artifacts in {run_dir}")


def _select_samples(samples: list[Any], max_samples: int | None) -> list[Any]:
    if max_samples is None or max_samples <= 0:
        return samples
    return samples[:max_samples]


async def command_run(args: argparse.Namespace) -> None:
    run_dir = await command_collect(args)
    evaluate_args = argparse.Namespace(
        config=args.config,
        dataset_path=args.dataset_path,
        run_dir=run_dir,
        max_workers=args.max_workers,
    )
    command_evaluate(evaluate_args)


async def command_demo(args: argparse.Namespace) -> None:
    from fastmcp import Client

    config = EvalConfig.from_toml(args.config)
    if args.question:
        queries = demo_queries_from_cli(args.question)
    else:
        queries = load_demo_queries(args.queries)
    queries = _select_samples(queries, args.limit)
    if not queries:
        print("No demo queries selected.")
        return

    if not args.no_preflight:
        preflight = await live_preflight(config)
        print(
            f"Preflight OK: tool={preflight.tool_name} "
            f"return_contexts={preflight.return_contexts_supported}"
        )

    failures = 0
    async with Client(config.mcp_url) as client:
        for index, query in enumerate(queries, start=1):
            started = time.monotonic()
            try:
                raw_response = await asyncio.wait_for(
                    client.call_tool(
                        config.tool_name,
                        {"user_query": query.question, "return_contexts": True},
                    ),
                    timeout=config.inference.timeout_seconds,
                )
                response = parse_rag_asap_response(raw_response, contexts_requested=True)
                print(
                    render_demo_response(
                        index=index,
                        total=len(queries),
                        query=query,
                        response=response,
                        latency_seconds=time.monotonic() - started,
                        max_contexts=args.max_contexts,
                        max_demonstrations=args.max_demonstrations,
                        snippet_chars=args.snippet_chars,
                    )
                )
            except Exception as exc:
                failures += 1
                print(
                    render_demo_error(
                        index=index,
                        total=len(queries),
                        query=query,
                        latency_seconds=time.monotonic() - started,
                        error=exc,
                    )
                )

    if failures:
        raise RuntimeError(f"{failures} demo query call(s) failed.")


def evaluate_run_dir(config: EvalConfig, run_dir: Path) -> dict[str, Any]:
    audit, _samples = load_dataset(config.dataset_path)
    records = read_jsonl(run_dir / "inference_samples.jsonl", InferenceRecord)
    started = datetime.now(timezone.utc)
    ragas_result = run_ragas_evaluation(records, config)
    finished = datetime.now(timezone.utc)
    summary = write_evaluation_artifacts(
        run_dir,
        records,
        ragas_result,
        audit=audit,
        started_at=started,
        finished_at=finished,
    )
    manifest_path = run_dir / "run_manifest.json"
    existing: dict[str, Any] = {}
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
    existing.update(
        {
            "ragas_version": ragas.__version__,
            "ragas_metric_names": ragas_result.metric_names,
            "judge_llm_model_name": config.judge.llm_model_name,
            "judge_embed_model_name": config.judge.embed_model_name,
            "sanitized_config": config.sanitized_model_dump(),
            "evaluation_finished_at": finished.isoformat(),
            "summary": {
                "total_samples": summary["total_samples"],
                "ragas_eligible_count": summary["ragas_eligible_count"],
                "ragas_scored_count": summary["ragas_scored_count"],
            },
        }
    )
    write_manifest(manifest_path, existing)
    return summary


async def live_preflight(config: EvalConfig) -> PreflightResult:
    import httpx
    from fastmcp import Client

    base_url = _http_base_url(config.mcp_url)
    async with httpx.AsyncClient(timeout=10) as http_client:
        ping_response = await http_client.get(f"{base_url}/ping")
        ping_response.raise_for_status()
        config_response = await http_client.get(f"{base_url}/config")
        config_response.raise_for_status()
        component_config = config_response.json()

    async with Client(config.mcp_url) as mcp_client:
        tools = await mcp_client.list_tools()
    tool = _find_tool(tools, config.tool_name)
    if tool is None:
        raise RuntimeError(f"MCP tool {config.tool_name!r} was not found")
    if not _tool_has_parameter(tool, "return_contexts"):
        raise RuntimeError(
            f"MCP tool {config.tool_name!r} does not expose 'return_contexts'"
        )
    return PreflightResult(
        ping={"status_code": ping_response.status_code},
        config=component_config,
        tool_name=config.tool_name,
        return_contexts_supported=True,
    )


def _client_factory(mcp_url: str):
    from fastmcp import Client

    return lambda: Client(mcp_url)


def build_run_manifest(
    *,
    config: EvalConfig,
    audit: BaseModel,
    selected_sample_count: int,
    run_dir: Path,
    mcp_preflight: PreflightResult | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "run_id": run_dir.name,
        "created_at": now.isoformat(),
        "dataset": audit.model_dump(mode="json"),
        "selected_sample_count": selected_sample_count,
        "mcp_url": config.mcp_url,
        "tool_name": config.tool_name,
        "mcp_preflight": (
            mcp_preflight.model_dump(mode="json") if mcp_preflight is not None else None
        ),
        "component_git": component_git_state(),
        "ragas_version": ragas.__version__,
        "ragas_metric_names": RAGAS_METRIC_NAMES,
        "judge_llm_model_name": config.judge.llm_model_name,
        "judge_embed_model_name": config.judge.embed_model_name,
        "sanitized_config": config.sanitized_model_dump(),
    }


def component_git_state() -> dict[str, Any]:
    repo = Path(__file__).resolve().parents[3] / "self-service-orig"
    if not repo.exists():
        return {"repo": str(repo), "available": False}
    commit = _git_output(repo, ["git", "rev-parse", "HEAD"])
    dirty = _git_output(repo, ["git", "status", "--short"])
    return {
        "repo": str(repo),
        "available": commit is not None,
        "commit": commit,
        "dirty": bool(dirty),
    }


def _git_output(repo: Path, command: list[str]) -> str | None:
    try:
        result = subprocess.run(
            command,
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip()


def _http_base_url(mcp_url: str) -> str:
    parts = urlsplit(mcp_url)
    path = parts.path.rstrip("/")
    if path.endswith("/mcp"):
        path = path[: -len("/mcp")]
    return urlunsplit((parts.scheme, parts.netloc, path.rstrip("/"), "", "")).rstrip("/")


def _find_tool(tools: list[Any], tool_name: str) -> Any | None:
    for tool in tools:
        if _field(tool, "name") == tool_name:
            return tool
    return None


def _tool_has_parameter(tool: Any, parameter: str) -> bool:
    schema = _field(tool, "inputSchema") or _field(tool, "input_schema") or {}
    if isinstance(schema, BaseModel):
        schema = schema.model_dump(mode="python")
    if hasattr(schema, "model_dump"):
        schema = schema.model_dump(mode="python")
    return isinstance(schema, dict) and parameter in (schema.get("properties") or {})


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _add_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, required=True)


def _add_dataset_path_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dataset-path",
        type=Path,
        default=None,
        help="Override dataset_path from config.toml.",
    )


def _load_config(args: argparse.Namespace) -> EvalConfig:
    config = EvalConfig.from_toml(args.config)
    dataset_path = getattr(args, "dataset_path", None)
    if dataset_path is not None:
        config = config.model_copy(update={"dataset_path": dataset_path})
    return config
