#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/launch-test-models.sh [options]

Launch local vLLM servers used for component testing.

By default, the launcher starts the servers in the background, waits until all
endpoints are ready, writes a PID state file, prints the stop command, and exits.

Options:
  --foreground                  Keep the launcher attached. Ctrl-C stops servers.
  --no-wait                     Start servers and exit without readiness checks.
  --stop                        Stop servers recorded in the PID state file.
  -h, --help                    Show this help.

Started by default:
  Qwen/Qwen2.5-32B-Instruct on CUDA device 0, port 7114
  Qwen/Qwen2.5-7B-Instruct on CUDA device 1, port 7113
  jinaai/jina-embeddings-v3 on CUDA device 1, port 3300

Environment overrides:
  VLLM_BIN                    vLLM executable override, default: uv run --no-sync vllm.
  VLLM_USE_FLASHINFER_SAMPLER FlashInfer sampler toggle, default: 0.
  TEST_MODEL_LOG_DIR          Log directory, default: logs/vllm.
  TEST_MODEL_READY_TIMEOUT    Seconds to wait for each server, default: 7200.
  TEST_MODEL_READY_INTERVAL   Seconds between readiness checks, default: 5.
  TEST_MODEL_CURL_TIMEOUT     Seconds per readiness request, default: 5.
  TEST_MODEL_STATE_FILE       PID state file, default: TEST_MODEL_LOG_DIR/test-models.tsv.

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

FOREGROUND=0
STOP_MODE=0
WAIT_FOR_READY=1

while (($#)); do
  case "$1" in
    --foreground)
      FOREGROUND=1
      ;;
    --no-wait)
      WAIT_FOR_READY=0
      ;;
    --stop)
      STOP_MODE=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      log "ERROR: unknown option: $1"
      usage
      exit 2
      ;;
  esac
  shift
done

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
EVAL_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
cd "$EVAL_ROOT"

TEST_MODEL_LOG_DIR="${TEST_MODEL_LOG_DIR:-logs/vllm}"
VLLM_USE_FLASHINFER_SAMPLER="${VLLM_USE_FLASHINFER_SAMPLER:-0}"
TEST_MODEL_READY_TIMEOUT="${TEST_MODEL_READY_TIMEOUT:-7200}"
TEST_MODEL_READY_INTERVAL="${TEST_MODEL_READY_INTERVAL:-5}"
TEST_MODEL_CURL_TIMEOUT="${TEST_MODEL_CURL_TIMEOUT:-5}"

absolutize_path() {
  local path="$1"

  if [[ "$path" == /* ]]; then
    printf '%s\n' "$path"
  else
    printf '%s/%s\n' "$EVAL_ROOT" "$path"
  fi
}

TEST_MODEL_LOG_DIR="$(absolutize_path "$TEST_MODEL_LOG_DIR")"
TEST_MODEL_STATE_FILE="${TEST_MODEL_STATE_FILE:-$TEST_MODEL_LOG_DIR/test-models.tsv}"
TEST_MODEL_STATE_FILE="$(absolutize_path "$TEST_MODEL_STATE_FILE")"

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
mkdir -p "$(dirname -- "$TEST_MODEL_STATE_FILE")"

state_names=()
state_pids=()
state_ports=()
state_log_files=()
state_run_ids=()

load_state_file() {
  state_names=()
  state_pids=()
  state_ports=()
  state_log_files=()
  state_run_ids=()

  [[ -f "$TEST_MODEL_STATE_FILE" ]] || return 1

  local name pid port log_file run_id
  while IFS=$'\t' read -r name pid port log_file run_id; do
    [[ -n "${name}${pid}${port}${log_file}" ]] || continue
    if [[ ! "$pid" =~ ^[0-9]+$ ]]; then
      log "WARN: skipping invalid PID state entry for $name: $pid"
      continue
    fi
    state_names+=("$name")
    state_pids+=("$pid")
    state_ports+=("$port")
    state_log_files+=("$log_file")
    state_run_ids+=("$run_id")
  done <"$TEST_MODEL_STATE_FILE"
}

is_process_running() {
  local pid="$1"

  kill -0 "$pid" 2>/dev/null
}

process_group_id() {
  local pid="$1"

  ps -o pgid= -p "$pid" 2>/dev/null | tr -d '[:space:]' || true
}

send_signal_to_pid_or_group() {
  local signal="$1"
  local pid="$2"
  local pgid

  pgid="$(process_group_id "$pid")"
  if [[ "$pgid" == "$pid" ]]; then
    kill "-$signal" -- "-$pid" 2>/dev/null || true
  else
    kill "-$signal" "$pid" 2>/dev/null || true
  fi
}

wait_for_process_exit() {
  local pid="$1"
  local deadline=$((SECONDS + 20))

  while ((SECONDS < deadline)); do
    if ! is_process_running "$pid"; then
      return 0
    fi
    sleep 1
  done

  return 1
}

is_tracked_model_process() {
  local pid="$1"
  local run_id="$2"

  [[ -n "$run_id" ]] || return 1
  [[ -r "/proc/$pid/environ" ]] || return 1

  tr '\0' '\n' <"/proc/$pid/environ" 2>/dev/null \
    | grep -Fx "ASAP_EVAL_TEST_MODEL_RUN_ID=$run_id" >/dev/null 2>&1
}

stop_pids() {
  local pid

  for pid in "$@"; do
    is_process_running "$pid" || continue
    send_signal_to_pid_or_group TERM "$pid"
  done

  for pid in "$@"; do
    is_process_running "$pid" || continue
    if ! wait_for_process_exit "$pid"; then
      log "WARN: process $pid did not stop after SIGTERM; sending SIGKILL."
      send_signal_to_pid_or_group KILL "$pid"
    fi
  done
}

shell_quote() {
  printf '%q' "$1"
}

print_stop_command() {
  log "Stop model servers with:"
  log "  TEST_MODEL_STATE_FILE=$(shell_quote "$TEST_MODEL_STATE_FILE") $(shell_quote "$SCRIPT_DIR/launch-test-models.sh") --stop"
}

stop_tracked_servers() {
  if ! load_state_file; then
    log "No model state file found: $TEST_MODEL_STATE_FILE"
    return 0
  fi

  if ((${#state_pids[@]} == 0)); then
    log "No model PIDs found in: $TEST_MODEL_STATE_FILE"
    rm -f "$TEST_MODEL_STATE_FILE"
    return 0
  fi

  local pids_to_stop=()
  local i
  for i in "${!state_pids[@]}"; do
    if is_tracked_model_process "${state_pids[$i]}" "${state_run_ids[$i]}"; then
      log "Stopping ${state_names[$i]} pid=${state_pids[$i]} port=${state_ports[$i]}"
      pids_to_stop+=("${state_pids[$i]}")
    elif is_process_running "${state_pids[$i]}"; then
      log "Skipping ${state_names[$i]} pid=${state_pids[$i]} port=${state_ports[$i]} because it does not match this launcher state."
    else
      log "${state_names[$i]} is not running pid=${state_pids[$i]} port=${state_ports[$i]}"
    fi
  done

  if ((${#pids_to_stop[@]} > 0)); then
    stop_pids "${pids_to_stop[@]}"
  fi

  rm -f "$TEST_MODEL_STATE_FILE"
  log "Removed model state file: $TEST_MODEL_STATE_FILE"
}

ensure_no_active_state_file() {
  [[ -f "$TEST_MODEL_STATE_FILE" ]] || return 0

  load_state_file || return 0

  local active_entries=()
  local i
  for i in "${!state_pids[@]}"; do
    if is_tracked_model_process "${state_pids[$i]}" "${state_run_ids[$i]}"; then
      active_entries+=("${state_names[$i]}:${state_pids[$i]}")
    fi
  done

  if ((${#active_entries[@]} > 0)); then
    log "ERROR: model servers already appear to be running: ${active_entries[*]}"
    print_stop_command
    exit 1
  fi

  log "Removing stale model state file: $TEST_MODEL_STATE_FILE"
  rm -f "$TEST_MODEL_STATE_FILE"
}

if ((STOP_MODE == 1)); then
  stop_tracked_servers
  exit 0
fi

if [[ -n "${VLLM_BIN:-}" ]]; then
  vllm_cmd=("$VLLM_BIN")
  vllm_preflight=("$VLLM_BIN" --version)
else
  vllm_cmd=(uv run --no-sync vllm)
  vllm_preflight=(uv run --no-sync python -c 'import importlib.util, sys; sys.exit(0 if importlib.util.find_spec("vllm") else 1)')
fi

log "Using vLLM command: ${vllm_cmd[*]}"
log "Using VLLM_USE_FLASHINFER_SAMPLER=$VLLM_USE_FLASHINFER_SAMPLER"
if ! "${vllm_preflight[@]}" >/dev/null 2>&1; then
  log "ERROR: vLLM command is not available: ${vllm_cmd[*]}"
  log "Install vLLM in this project environment with: uv pip install vllm"
  log "Or point VLLM_BIN at an existing executable, for example: VLLM_BIN=/path/to/vllm $0"
  exit 1
fi

ensure_no_active_state_file

pids=()
names=()
ports=()
log_files=()
TEST_MODEL_RUN_ID="$(date +%Y%m%d%H%M%S)-$$"
STATE_FILE_WRITTEN=0
CLEANUP_ON_EXIT=1

cleanup() {
  local exit_code=$?

  if ((CLEANUP_ON_EXIT == 1 && ${#pids[@]} > 0)); then
    log "Stopping model servers: ${pids[*]}"
    stop_pids "${pids[@]}"
    wait "${pids[@]}" 2>/dev/null || true
  fi
  if ((STATE_FILE_WRITTEN == 1 && CLEANUP_ON_EXIT == 1)); then
    rm -f "$TEST_MODEL_STATE_FILE"
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
  if command -v setsid >/dev/null 2>&1; then
    setsid env \
      ASAP_EVAL_TEST_MODEL_NAME="$name" \
      ASAP_EVAL_TEST_MODEL_RUN_ID="$TEST_MODEL_RUN_ID" \
      "$@" >"$log_file" 2>&1 </dev/null &
  else
    nohup env \
      ASAP_EVAL_TEST_MODEL_NAME="$name" \
      ASAP_EVAL_TEST_MODEL_RUN_ID="$TEST_MODEL_RUN_ID" \
      "$@" >"$log_file" 2>&1 </dev/null &
  fi
  pids+=("$!")
  names+=("$name")
  ports+=("$port")
  log_files+=("$log_file")
}

write_state_file() {
  local tmp_file="${TEST_MODEL_STATE_FILE}.tmp"
  local i

  : >"$tmp_file"
  for i in "${!names[@]}"; do
    printf '%s\t%s\t%s\t%s\t%s\n' \
      "${names[$i]}" "${pids[$i]}" "${ports[$i]}" "${log_files[$i]}" "$TEST_MODEL_RUN_ID" >>"$tmp_file"
  done
  mv "$tmp_file" "$TEST_MODEL_STATE_FILE"
  STATE_FILE_WRITTEN=1
  log "Wrote model state file: $TEST_MODEL_STATE_FILE"
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
    if ! is_child_job_running "$pid"; then
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

is_child_job_running() {
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

if ((WAIT_FOR_READY == 1)); then
  for i in "${!names[@]}"; do
    wait_for_server "${names[$i]}" "${ports[$i]}" "${log_files[$i]}" "${pids[$i]}"
  done

  log "All model servers are ready."
fi

write_state_file
print_stop_command

if ((FOREGROUND == 0)); then
  CLEANUP_ON_EXIT=0
  trap - EXIT INT TERM
  for pid in "${pids[@]}"; do
    disown "$pid" 2>/dev/null || true
  done
  log "Launcher exiting; model servers remain running in the background."
  exit 0
fi

log "Foreground mode enabled. Press Ctrl-C to stop all model servers."

if wait -n "${pids[@]}"; then
  log "A model server exited; stopping the remaining servers."
else
  status=$?
  log "ERROR: a model server exited with status $status; stopping the remaining servers."
  exit "$status"
fi
