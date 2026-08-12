#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/init-rag-tool-asap.sh [options]

Prepare and start the rag_tool_asap component for evaluation.

Options:
  --skip-infra       Do not start OpenSearch and MinIO.
  --skip-upload      Do not upload the canonical ASAP CSV to MinIO.
  --skip-component   Do not start the rag_tool_asap MCP service.
  --no-wait          Do not wait for http://localhost:8100/ping.
  -h, --help         Show this help.

Environment overrides:
  ORIG_ROOT                         Path to self-service-asap.
  DATASET                           Path to canonical ASAP CSV.
  MINIO_WAIT_TIMEOUT_SECONDS        MinIO wait timeout, default: 120.
  COMPONENT_WAIT_TIMEOUT_SECONDS    Readiness wait timeout, default: 7200.
  STREAM_COMPONENT_LOGS             Stream component logs while waiting, default: 1.
EOF
}

log() {
  printf '[asap-eval:init] %s\n' "$*"
}

copy_example_if_missing() {
  local target="$1"
  local example="${target}.example"

  if [[ -f "$target" ]]; then
    return
  fi
  if [[ ! -f "$example" ]]; then
    log "WARN: template is missing: $example"
    return
  fi
  cp "$example" "$target"
  log "Created $target from $example. Fill real credentials before starting the component."
}

run_minio_mc() {
  docker run --rm --network self_service \
    -v "$MINIO_MC_DIR:/root/.mc" \
    minio/mc "$@"
}

wait_for_minio() {
  local timeout_seconds="$1"
  local started_at
  started_at="$(date +%s)"

  log "Waiting for MinIO readiness at $MINIO_ENDPOINT"
  while true; do
    if run_minio_mc alias set "$MINIO_ALIAS" "$MINIO_ENDPOINT" "$MINIO_ACCESS_KEY" "$MINIO_SECRET_KEY" >/dev/null 2>&1; then
      log "MinIO is ready."
      return
    fi

    local now elapsed
    now="$(date +%s)"
    elapsed=$((now - started_at))
    if (( elapsed >= timeout_seconds )); then
      log "ERROR: MinIO did not become ready in ${timeout_seconds}s."
      log "Check logs with: cd \"$ORIG_ROOT\" && docker compose logs -f minio-storage"
      return 1
    fi
    sleep 3
  done
}

show_component_logs() {
  if docker inspect "$COMPONENT_CONTAINER" >/dev/null 2>&1; then
    log "Recent component logs from $COMPONENT_CONTAINER:"
    docker logs --tail "${COMPONENT_LOG_TAIL:-120}" "$COMPONENT_CONTAINER" || true
  else
    log "Component container was not found: $COMPONENT_CONTAINER"
  fi
}

start_component_log_stream() {
  if [[ "${STREAM_COMPONENT_LOGS:-1}" != "1" ]]; then
    return
  fi
  if ! docker inspect "$COMPONENT_CONTAINER" >/dev/null 2>&1; then
    return
  fi
  log "Streaming component logs from $COMPONENT_CONTAINER while waiting."
  docker logs --follow --tail "${COMPONENT_LOG_TAIL:-80}" "$COMPONENT_CONTAINER" &
  COMPONENT_LOG_STREAM_PID="$!"
}

stop_component_log_stream() {
  if [[ -n "${COMPONENT_LOG_STREAM_PID:-}" ]]; then
    kill "$COMPONENT_LOG_STREAM_PID" >/dev/null 2>&1 || true
    wait "$COMPONENT_LOG_STREAM_PID" >/dev/null 2>&1 || true
    COMPONENT_LOG_STREAM_PID=""
  fi
}

wait_for_component() {
  local url="$1"
  local timeout_seconds="$2"
  local started_at
  started_at="$(date +%s)"

  log "Waiting for component readiness at $url"
  start_component_log_stream
  while true; do
    if curl --fail --silent "$url" >/dev/null 2>&1; then
      stop_component_log_stream
      log "Component is ready."
      return
    fi

    local container_status
    container_status="$(docker inspect --format '{{.State.Status}}' "$COMPONENT_CONTAINER" 2>/dev/null || true)"
    if [[ "$container_status" == "exited" || "$container_status" == "dead" ]]; then
      stop_component_log_stream
      log "ERROR: component container stopped before readiness: status=$container_status"
      show_component_logs
      return 1
    fi

    local now elapsed
    now="$(date +%s)"
    elapsed=$((now - started_at))
    if (( elapsed >= timeout_seconds )); then
      stop_component_log_stream
      log "ERROR: component did not become ready in ${timeout_seconds}s."
      log "Check logs with: cd \"$ORIG_ROOT/scenarios\" && docker compose -f compose.common.yaml logs -f base_rag_tool_asap"
      show_component_logs
      return 1
    fi
    sleep 10
  done
}

SKIP_INFRA=0
SKIP_UPLOAD=0
SKIP_COMPONENT=0
WAIT_FOR_COMPONENT=1

while (($#)); do
  case "$1" in
    --skip-infra)
      SKIP_INFRA=1
      ;;
    --skip-upload)
      SKIP_UPLOAD=1
      ;;
    --skip-component)
      SKIP_COMPONENT=1
      ;;
    --no-wait)
      WAIT_FOR_COMPONENT=0
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
ORIG_ROOT="${ORIG_ROOT:-$EVAL_ROOT/../self-service-asap}"
COMPONENT_HEALTH_URL="${COMPONENT_HEALTH_URL:-http://localhost:8100/ping}"
COMPONENT_WAIT_TIMEOUT_SECONDS="${COMPONENT_WAIT_TIMEOUT_SECONDS:-7200}"
COMPONENT_CONTAINER="${COMPONENT_CONTAINER:-scenarios-base_rag_tool_asap-1}"

if [[ ! -d "$ORIG_ROOT" ]]; then
  log "ERROR: self-service-asap was not found at $ORIG_ROOT"
  exit 1
fi
ORIG_ROOT="$(cd -- "$ORIG_ROOT" && pwd)"
COMPONENT_DIR="$ORIG_ROOT/services/components/rag_tool_asap"
DATASET="${DATASET:-$COMPONENT_DIR/tests/files/asap.csv}"
MINIO_MC_DIR="${MINIO_MC_DIR:-$ORIG_ROOT/.mc}"
MINIO_ALIAS="${MINIO_ALIAS:-minio}"
MINIO_ENDPOINT="${MINIO_ENDPOINT:-http://minio-storage:9000}"
MINIO_ACCESS_KEY="${MINIO_ACCESS_KEY:-my_login}"
MINIO_SECRET_KEY="${MINIO_SECRET_KEY:-my_password}"
MINIO_BUCKET="${MINIO_BUCKET:-datasets}"
MINIO_OBJECT="${MINIO_OBJECT:-rag_tool_asap/doc/$(basename "$DATASET")}"
MINIO_WAIT_TIMEOUT_SECONDS="${MINIO_WAIT_TIMEOUT_SECONDS:-120}"
if [[ ! -f "$DATASET" ]]; then
  log "ERROR: dataset was not found at $DATASET"
  exit 1
fi

command -v docker >/dev/null || {
  log "ERROR: docker is required."
  exit 1
}
command -v curl >/dev/null || {
  log "ERROR: curl is required."
  exit 1
}

log "Evaluation root: $EVAL_ROOT"
log "Component root: $COMPONENT_DIR"
log "Dataset: $DATASET"

copy_example_if_missing "$ORIG_ROOT/secrets/minio.env"
copy_example_if_missing "$ORIG_ROOT/secrets/opensearch.env"
copy_example_if_missing "$ORIG_ROOT/secrets/llm.env"
copy_example_if_missing "$ORIG_ROOT/secrets/description_gen.llm.env"
copy_example_if_missing "$ORIG_ROOT/secrets/embedder.llm.env"

if (( SKIP_INFRA == 0 )); then
  log "Starting OpenSearch and MinIO from self-service-asap."
  (
    cd "$ORIG_ROOT"
    docker compose up -d opensearch minio-storage
  )
else
  log "Skipping infra startup."
fi

if (( SKIP_UPLOAD == 0 )); then
  log "Uploading canonical dataset to MinIO: $MINIO_BUCKET/$MINIO_OBJECT"
  mkdir -p "$MINIO_MC_DIR"
  wait_for_minio "$MINIO_WAIT_TIMEOUT_SECONDS"
  run_minio_mc mb -p "$MINIO_ALIAS/$MINIO_BUCKET"
  docker run --rm --network self_service \
    -v "$MINIO_MC_DIR:/root/.mc" \
    -v "$DATASET:/data/$(basename "$DATASET"):ro" \
    minio/mc cp "/data/$(basename "$DATASET")" "$MINIO_ALIAS/$MINIO_BUCKET/$MINIO_OBJECT"
  run_minio_mc ls "$MINIO_ALIAS/$MINIO_BUCKET/rag_tool_asap/doc"
else
  log "Skipping dataset upload."
fi

if (( SKIP_COMPONENT == 0 )); then
  log "Starting rag_tool_asap MCP component on http://localhost:8100."
  log "Make sure LLM, preprocessing LLM and embedding endpoints from self-service-asap/secrets/*.env are reachable."
  (
    cd "$ORIG_ROOT/scenarios"
    docker compose -f compose.common.yaml up --build --force-recreate -d base_rag_tool_asap
  )

  if (( WAIT_FOR_COMPONENT == 1 )); then
    wait_for_component "$COMPONENT_HEALTH_URL" "$COMPONENT_WAIT_TIMEOUT_SECONDS"
  else
    log "Component started in detached mode; readiness wait skipped."
  fi
else
  log "Skipping component startup."
fi

log "Initialization finished."
