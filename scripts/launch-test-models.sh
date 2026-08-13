#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/launch-test-models.sh

Launch local vLLM servers used for component testing.

Started by default:
  Qwen/Qwen2.5-32B-Instruct on CUDA device 0, port 7114
  Qwen/Qwen2.5-7B-Instruct on CUDA device 1, port 7113
  jinaai/jina-embeddings-v3 on CUDA device 1, port 3300

Environment overrides:
  VLLM_BIN                    vLLM executable override, default: uv run --no-sync vllm.
  VLLM_USE_FLASHINFER_SAMPLER FlashInfer sampler toggle, default: 0.
  TEST_MODEL_LOG_DIR          Log directory, default: logs/vllm.
  TEST_MODEL_READY_TIMEOUT    Seconds to wait for each server, default: 3600.
  TEST_MODEL_READY_INTERVAL   Seconds between readiness checks, default: 5.
  TEST_MODEL_CURL_TIMEOUT     Seconds per readiness request, default: 5.

  LARGE_LM_MODEL              Default: Qwen/Qwen2.5-32B-Instruct.
  LARGE_LM_CUDA_VISIBLE_DEVICES
                              Default: 0.
  LARGE_LM_PORT               Default: 7114.
  LARGE_LM_MAX_BATCHED_TOKENS Default: 8192.
  LARGE_LM_MAX_MODEL_LEN      Default: 8192.

  SMALL_LM_MODEL              Default: Qwen/Qwen2.5-7B-Instruct.
  SMALL_LM_CUDA_VISIBLE_DEVICES
                              Default: 1.
  SMALL_LM_PORT               Default: 7113.
  SMALL_LM_MAX_BATCHED_TOKENS Default: 8192.
  SMALL_LM_MAX_MODEL_LEN      Default: 8192.

  EMB_EMBEDDINGS_MODEL        Default: jinaai/jina-embeddings-v3.
  EMB_CUDA_VISIBLE_DEVICES    Default: 1.
  EMB_PORT                    Default: 3300.
  EMB_GPU_MEMORY_UTILIZATION  Default: 0.05.
EOF
}

log() {
  printf '[asap-eval:models] %s\n' "$*"
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
EVAL_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
cd "$EVAL_ROOT"

if [[ -n "${VLLM_BIN:-}" ]]; then
  vllm_cmd=("$VLLM_BIN")
  vllm_preflight=("$VLLM_BIN" --version)
else
  vllm_cmd=(uv run --no-sync vllm)
  vllm_preflight=(uv run --no-sync python -c 'import importlib.util, sys; sys.exit(0 if importlib.util.find_spec("vllm") else 1)')
fi

TEST_MODEL_LOG_DIR="${TEST_MODEL_LOG_DIR:-logs/vllm}"
VLLM_USE_FLASHINFER_SAMPLER="${VLLM_USE_FLASHINFER_SAMPLER:-0}"
TEST_MODEL_READY_TIMEOUT="${TEST_MODEL_READY_TIMEOUT:-3600}"
TEST_MODEL_READY_INTERVAL="${TEST_MODEL_READY_INTERVAL:-5}"
TEST_MODEL_CURL_TIMEOUT="${TEST_MODEL_CURL_TIMEOUT:-5}"

LARGE_LM_MODEL="${LARGE_LM_MODEL:-Qwen/Qwen2.5-32B-Instruct}"
LARGE_LM_CUDA_VISIBLE_DEVICES="${LARGE_LM_CUDA_VISIBLE_DEVICES:-0}"
LARGE_LM_PORT="${LARGE_LM_PORT:-7114}"
LARGE_LM_MAX_BATCHED_TOKENS="${LARGE_LM_MAX_BATCHED_TOKENS:-8192}"
LARGE_LM_MAX_MODEL_LEN="${LARGE_LM_MAX_MODEL_LEN:-8192}"

SMALL_LM_MODEL="${SMALL_LM_MODEL:-Qwen/Qwen2.5-7B-Instruct}"
SMALL_LM_CUDA_VISIBLE_DEVICES="${SMALL_LM_CUDA_VISIBLE_DEVICES:-1}"
SMALL_LM_PORT="${SMALL_LM_PORT:-7113}"
SMALL_LM_MAX_BATCHED_TOKENS="${SMALL_LM_MAX_BATCHED_TOKENS:-8192}"
SMALL_LM_MAX_MODEL_LEN="${SMALL_LM_MAX_MODEL_LEN:-8192}"

EMB_EMBEDDINGS_MODEL="${EMB_EMBEDDINGS_MODEL:-jinaai/jina-embeddings-v3}"
EMB_CUDA_VISIBLE_DEVICES="${EMB_CUDA_VISIBLE_DEVICES:-1}"
EMB_PORT="${EMB_PORT:-3300}"
EMB_GPU_MEMORY_UTILIZATION="${EMB_GPU_MEMORY_UTILIZATION:-0.05}"

mkdir -p "$TEST_MODEL_LOG_DIR"

log "Using vLLM command: ${vllm_cmd[*]}"
log "Using VLLM_USE_FLASHINFER_SAMPLER=$VLLM_USE_FLASHINFER_SAMPLER"
if ! "${vllm_preflight[@]}" >/dev/null 2>&1; then
  log "ERROR: vLLM command is not available: ${vllm_cmd[*]}"
  log "Install vLLM in this project environment with: uv pip install vllm"
  log "Or point VLLM_BIN at an existing executable, for example: VLLM_BIN=/path/to/vllm $0"
  exit 1
fi

pids=()
names=()
ports=()
log_files=()

cleanup() {
  local exit_code=$?

  if ((${#pids[@]} > 0)); then
    log "Stopping model servers: ${pids[*]}"
    kill "${pids[@]}" 2>/dev/null || true
    wait "${pids[@]}" 2>/dev/null || true
  fi

  exit "$exit_code"
}

trap cleanup EXIT INT TERM

start_server() {
  local name="$1"
  local port="$2"
  shift 2
  local log_file="$TEST_MODEL_LOG_DIR/$name.log"

  log "Starting $name. Logs: $log_file"
  "$@" >"$log_file" 2>&1 &
  pids+=("$!")
  names+=("$name")
  ports+=("$port")
  log_files+=("$log_file")
}

wait_for_server() {
  local name="$1"
  local port="$2"
  local log_file="$3"
  local pid="$4"
  local url="http://localhost:$port/v1/models"
  local deadline=$((SECONDS + TEST_MODEL_READY_TIMEOUT))

  log "Waiting for $name at $url"

  while ((SECONDS < deadline)); do
    if ! is_pid_running "$pid"; then
      log "ERROR: $name process exited before becoming ready. Recent logs:"
      print_log_excerpt "$log_file"
      return 1
    fi

    if curl --fail --silent --show-error --max-time "$TEST_MODEL_CURL_TIMEOUT" "$url" >/dev/null 2>&1; then
      log "$name is ready at $url"
      return 0
    fi

    sleep "$TEST_MODEL_READY_INTERVAL"
  done

  log "ERROR: $name did not become ready within ${TEST_MODEL_READY_TIMEOUT}s. Recent logs:"
  print_log_excerpt "$log_file"
  return 1
}

print_log_excerpt() {
  local log_file="$1"

  if [[ ! -f "$log_file" ]]; then
    log "No log file found: $log_file"
    return 0
  fi

  grep -iE "fatal error|error:|runtimeerror|exception|traceback|no such file|out of memory|failed" "$log_file" \
    | tail -n 40 >&2 || true
  tail -n 80 "$log_file" >&2 || true
}

is_pid_running() {
  local pid="$1"
  local running_pid

  for running_pid in $(jobs -pr); do
    if [[ "$running_pid" == "$pid" ]]; then
      return 0
    fi
  done

  return 1
}

start_server large-lm "$LARGE_LM_PORT" \
  env CUDA_VISIBLE_DEVICES="$LARGE_LM_CUDA_VISIBLE_DEVICES" \
  VLLM_USE_FLASHINFER_SAMPLER="$VLLM_USE_FLASHINFER_SAMPLER" \
  "${vllm_cmd[@]}" serve "$LARGE_LM_MODEL" \
  --port "$LARGE_LM_PORT" \
  --max-num-batched-tokens "$LARGE_LM_MAX_BATCHED_TOKENS" \
  --max-model-len "$LARGE_LM_MAX_MODEL_LEN"

start_server small-lm "$SMALL_LM_PORT" \
  env CUDA_VISIBLE_DEVICES="$SMALL_LM_CUDA_VISIBLE_DEVICES" \
  VLLM_USE_FLASHINFER_SAMPLER="$VLLM_USE_FLASHINFER_SAMPLER" \
  "${vllm_cmd[@]}" serve "$SMALL_LM_MODEL" \
  --port "$SMALL_LM_PORT" \
  --max-num-batched-tokens "$SMALL_LM_MAX_BATCHED_TOKENS" \
  --max-model-len "$SMALL_LM_MAX_MODEL_LEN"

start_server jina-embeddings "$EMB_PORT" \
  env CUDA_VISIBLE_DEVICES="$EMB_CUDA_VISIBLE_DEVICES" \
  VLLM_USE_FLASHINFER_SAMPLER="$VLLM_USE_FLASHINFER_SAMPLER" \
  "${vllm_cmd[@]}" serve "$EMB_EMBEDDINGS_MODEL" \
  --port "$EMB_PORT" \
  --trust-remote-code \
  --gpu-memory-utilization "$EMB_GPU_MEMORY_UTILIZATION"

log "Started model servers: ${pids[*]}"

for i in "${!names[@]}"; do
  wait_for_server "${names[$i]}" "${ports[$i]}" "${log_files[$i]}" "${pids[$i]}"
done

log "All model servers are ready."
log "Press Ctrl-C to stop all model servers."

if wait -n "${pids[@]}"; then
  log "A model server exited; stopping the remaining servers."
else
  status=$?
  log "ERROR: a model server exited with status $status; stopping the remaining servers."
  exit "$status"
fi
