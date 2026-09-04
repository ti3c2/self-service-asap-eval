#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: self-service-utils/scripts/cleanup-rag-tool-asap-storage.sh [options]

Clean only storage created for rag_tool_asap.

By default this is a dry run. Pass --yes to delete.

Targets:
  OpenSearch docs: documents in ${INDEX_PREFIX}-chunks and
                   ${INDEX_PREFIX}-synthetic whose metadata.doc_hash matches
                   files under the selected MinIO prefixes.
  MinIO objects:   Files prefixes from the component config, for example
                   datasets/rag_tool_asap/doc/

Options:
  --yes                         Delete the selected resources.
  --dry-run                     Show what would be deleted. This is the default.
  --only opensearch|minio       Clean only one backend.
  --skip-opensearch             Do not touch OpenSearch.
  --skip-minio                  Do not touch MinIO.
  --orig-root PATH              Path to self-service-asap.
  --config PATH                 Component config JSON.
  --index-prefix PREFIX         OpenSearch index prefix. Default: rag-tool-asap.
  --delete-opensearch-indices   Delete ${INDEX_PREFIX}-chunks and ${INDEX_PREFIX}-synthetic entirely.
  --doc-hash HASH               OpenSearch doc_hash to remove. Can be repeated.
  --opensearch-url URL          Full OpenSearch URL. Default: http://$OPENSEARCH_HOST:$OPENSEARCH_PORT.
  --host-curl                   Use host curl for OpenSearch instead of Docker.
  --minio-endpoint ENDPOINT     MinIO endpoint. Default from env file or minio-storage:9000.
  --minio-target BUCKET/PREFIX  MinIO prefix to remove. Can be repeated.
  --docker-network NAME         Docker network for cleanup clients. Default: self_service.
  -h, --help                    Show this help.

Environment overrides:
  ORIG_ROOT, CONFIG_PATH, INDEX_PREFIX, OPENSEARCH_URL, OPENSEARCH_HOST,
  OPENSEARCH_PORT, OPENSEARCH_LOGIN, OPENSEARCH_PASSWORD, MINIO_ENDPOINT,
  MINIO_ACCESS_KEY, MINIO_SECRET_KEY, MINIO_ALIAS, MINIO_TARGETS,
  DOCKER_NETWORK, CURL_IMAGE, MINIO_IMAGE, MINIO_MC_DIR.

Examples:
  self-service-utils/scripts/cleanup-rag-tool-asap-storage.sh
  self-service-utils/scripts/cleanup-rag-tool-asap-storage.sh --yes
  INDEX_PREFIX=asap-eval-smoke self-service-utils/scripts/cleanup-rag-tool-asap-storage.sh --yes
EOF
}

log() {
  printf '[rag-tool-asap:cleanup] %s\n' "$*"
}

die() {
  printf '[rag-tool-asap:cleanup] ERROR: %s\n' "$*" >&2
  exit 1
}

require_value() {
  local option="$1"
  local value="${2:-}"
  if [[ -z "$value" ]]; then
    die "$option requires a value."
  fi
}

with_http_scheme() {
  local value="$1"
  case "$value" in
    http://*|https://*)
      printf '%s\n' "$value"
      ;;
    *)
      printf 'http://%s\n' "$value"
      ;;
  esac
}

load_env_file_preserving_overrides() {
  local file="$1"
  local line key value

  [[ -f "$file" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    [[ -z "$line" || "$line" == \#* ]] && continue
    [[ "$line" == export\ * ]] && line="${line#export }"
    [[ "$line" == *=* ]] || continue

    key="${line%%=*}"
    key="${key%"${key##*[![:space:]]}"}"
    value="${line#*=}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue

    if [[ -z "${!key+x}" ]]; then
      eval "export ${key}=${value}"
    fi
  done < "$file"
}

validate_index_prefix() {
  [[ -n "$INDEX_PREFIX" ]] || die "INDEX_PREFIX must not be empty."
  if [[ ! "$INDEX_PREFIX" =~ ^[a-z0-9][a-z0-9._-]*$ ]]; then
    die "INDEX_PREFIX must be a concrete lowercase OpenSearch index prefix, got: $INDEX_PREFIX"
  fi
}

run_opensearch_curl() {
  if (( USE_DOCKER_CURL == 1 )); then
    docker run --rm --network "$DOCKER_NETWORK" "$CURL_IMAGE" "$@"
  else
    curl "$@"
  fi
}

opensearch_status() {
  local method="$1"
  local path="$2"
  local url="${OPENSEARCH_URL%/}/${path#/}"
  local args=(
    --silent
    --show-error
    --output /dev/null
    --write-out '%{http_code}'
    --request "$method"
    --connect-timeout "${CURL_CONNECT_TIMEOUT_SECONDS:-5}"
    --max-time "${CURL_MAX_TIME_SECONDS:-30}"
  )

  if [[ -n "${OPENSEARCH_LOGIN:-}" || -n "${OPENSEARCH_PASSWORD:-}" ]]; then
    args+=(--user "${OPENSEARCH_LOGIN:-}:${OPENSEARCH_PASSWORD:-}")
  fi

  run_opensearch_curl "${args[@]}" "$url"
}

opensearch_delete() {
  local path="$1"
  local url="${OPENSEARCH_URL%/}/${path#/}"
  local args=(
    --silent
    --show-error
    --fail
    --request DELETE
    --connect-timeout "${CURL_CONNECT_TIMEOUT_SECONDS:-5}"
    --max-time "${CURL_MAX_TIME_SECONDS:-30}"
  )

  if [[ -n "${OPENSEARCH_LOGIN:-}" || -n "${OPENSEARCH_PASSWORD:-}" ]]; then
    args+=(--user "${OPENSEARCH_LOGIN:-}:${OPENSEARCH_PASSWORD:-}")
  fi

  run_opensearch_curl "${args[@]}" "$url"
}

opensearch_json_request() {
  local method="$1"
  local path="$2"
  local body="$3"
  local url="${OPENSEARCH_URL%/}/${path#/}"
  local args=(
    --silent
    --show-error
    --fail
    --request "$method"
    --header 'Content-Type: application/json'
    --data-binary "$body"
    --connect-timeout "${CURL_CONNECT_TIMEOUT_SECONDS:-5}"
    --max-time "${CURL_MAX_TIME_SECONDS:-30}"
  )

  if [[ -n "${OPENSEARCH_LOGIN:-}" || -n "${OPENSEARCH_PASSWORD:-}" ]]; then
    args+=(--user "${OPENSEARCH_LOGIN:-}:${OPENSEARCH_PASSWORD:-}")
  fi

  run_opensearch_curl "${args[@]}" "$url"
}

doc_hash_query_body() {
  python3 - "$@" <<'PY'
import json
import sys

print(json.dumps({"query": {"terms": {"metadata.doc_hash": sys.argv[1:]}}}))
PY
}

json_field_or_unknown() {
  local field="$1"
  python3 - "$field" <<'PY'
import json
import sys

field = sys.argv[1]
try:
    payload = json.load(sys.stdin)
except json.JSONDecodeError:
    print("?")
    raise SystemExit
value = payload.get(field, "?")
print(value)
PY
}

cleanup_opensearch_indices() {
  validate_index_prefix

  local suffix index status
  for suffix in chunks synthetic; do
    index="${INDEX_PREFIX}-${suffix}"
    status="$(opensearch_status GET "$index" || true)"

    case "$status" in
      200)
        if (( DRY_RUN == 1 )); then
          log "OpenSearch dry run: would delete index $index"
        else
          log "Deleting OpenSearch index $index"
          opensearch_delete "$index" >/dev/null
        fi
        ;;
      404)
        log "OpenSearch index not found, skipping: $index"
        ;;
      000|"")
        die "Could not reach OpenSearch at $OPENSEARCH_URL."
        ;;
      *)
        die "Unexpected OpenSearch status for $index: HTTP $status"
        ;;
    esac
  done
}

cleanup_opensearch_doc_hashes() {
  validate_index_prefix
  build_doc_hashes

  if ((${#DOC_HASH_LIST[@]} == 0)); then
    log "No document hashes found; skipping OpenSearch cleanup."
    return
  fi

  local body suffix index status response count deleted
  body="$(doc_hash_query_body "${DOC_HASH_LIST[@]}")"

  for suffix in chunks synthetic; do
    index="${INDEX_PREFIX}-${suffix}"
    status="$(opensearch_status GET "$index" || true)"

    case "$status" in
      200)
        if (( DRY_RUN == 1 )); then
          response="$(opensearch_json_request POST "$index/_count" "$body")"
          count="$(json_field_or_unknown count <<< "$response")"
          log "OpenSearch dry run: would delete $count docs from $index for ${#DOC_HASH_LIST[@]} doc_hash value(s)"
        else
          log "Deleting OpenSearch docs from $index for ${#DOC_HASH_LIST[@]} doc_hash value(s)"
          response="$(opensearch_json_request POST "$index/_delete_by_query?refresh=true&conflicts=proceed" "$body")"
          deleted="$(json_field_or_unknown deleted <<< "$response")"
          log "Deleted $deleted docs from $index"
        fi
        ;;
      404)
        log "OpenSearch index not found, skipping: $index"
        ;;
      000|"")
        die "Could not reach OpenSearch at $OPENSEARCH_URL."
        ;;
      *)
        die "Unexpected OpenSearch status for $index: HTTP $status"
        ;;
    esac
  done
}

run_mc() {
  docker run --rm --network "$DOCKER_NETWORK" \
    -v "$MINIO_MC_DIR:/root/.mc" \
    "$MINIO_IMAGE" "$@"
}

setup_minio_alias() {
  if (( MINIO_ALIAS_READY == 1 )); then
    return
  fi
  mkdir -p "$MINIO_MC_DIR"
  run_mc alias set "$MINIO_ALIAS" "$MINIO_ENDPOINT_URL" "$MINIO_ACCESS_KEY" "$MINIO_SECRET_KEY" >/dev/null
  MINIO_ALIAS_READY=1
}

add_doc_hash() {
  local doc_hash="$1"
  local existing

  if [[ ! "$doc_hash" =~ ^[A-Fa-f0-9]{64}$ ]]; then
    die "Invalid SHA-256 doc hash: $doc_hash"
  fi
  doc_hash="${doc_hash,,}"

  for existing in "${DOC_HASH_LIST[@]}"; do
    [[ "$existing" == "$doc_hash" ]] && return
  done
  DOC_HASH_LIST+=("$doc_hash")
}

list_minio_objects_for_target() {
  local target="$1"
  local bucket="${target%%/*}"
  local prefix="${target#*/}"
  local uri="$MINIO_ALIAS/$bucket/$prefix"
  local found_uri object

  if ! run_mc ls "$MINIO_ALIAS/$bucket" >/dev/null 2>&1; then
    printf '[rag-tool-asap:cleanup] MinIO bucket not found while deriving doc hashes, skipping: %s\n' "$bucket" >&2
    return
  fi

  run_mc find "$uri" 2>/dev/null | while IFS= read -r found_uri; do
    found_uri="${found_uri#"$MINIO_ALIAS/"}"
    object="${found_uri#"$bucket/"}"
    [[ -n "$object" && "$object" != "$found_uri" ]] || continue
    printf '%s/%s\n' "$bucket" "$object"
  done
}

hash_minio_object() {
  local object_spec="$1"
  local uri="$MINIO_ALIAS/$object_spec"

  run_mc cat "$uri" | python3 -c '
import hashlib
import sys

hasher = hashlib.sha256()
for chunk in iter(lambda: sys.stdin.buffer.read(1024 * 1024), b""):
    hasher.update(chunk)
print(hasher.hexdigest())
'
}

build_doc_hashes() {
  local raw_hash target object_spec object_hash
  DOC_HASH_LIST=()

  if ((${#DOC_HASH_ARGS[@]} > 0)); then
    for raw_hash in "${DOC_HASH_ARGS[@]}"; do
      add_doc_hash "$raw_hash"
    done
    return
  fi

  build_minio_targets
  if ((${#MINIO_TARGET_LIST[@]} == 0)); then
    return
  fi

  setup_minio_alias
  for target in "${MINIO_TARGET_LIST[@]}"; do
    while IFS= read -r object_spec; do
      [[ -n "$object_spec" ]] || continue
      object_hash="$(hash_minio_object "$object_spec")"
      add_doc_hash "$object_hash"
    done < <(list_minio_objects_for_target "$target")
  done
}

collect_config_minio_targets() {
  [[ -f "$CONFIG_PATH" ]] || die "Component config not found: $CONFIG_PATH"
  command -v python3 >/dev/null || die "python3 is required to parse $CONFIG_PATH."

  python3 - "$CONFIG_PATH" <<'PY'
import json
import sys
from pathlib import Path
from typing import Any

config_path = Path(sys.argv[1])
with config_path.open(encoding="utf-8") as fp:
    data = json.load(fp)

seen: set[str] = set()

def emit(target: str) -> None:
    if target not in seen:
        seen.add(target)
        print(target)

def walk(value: Any) -> None:
    if isinstance(value, dict):
        if {"bucket_name", "component_id", "field_name"} <= set(value):
            if value.get("mock_minio_server") is True:
                return
            bucket = str(value.get("bucket_name", "")).strip().strip("/")
            component_id = str(value.get("component_id", "")).strip().strip("/")
            field_name = str(value.get("field_name", "")).strip().strip("/")
            if bucket and component_id and field_name:
                emit(f"{bucket}/{component_id}/{field_name}/")
        for child in value.values():
            walk(child)
    elif isinstance(value, list):
        for child in value:
            walk(child)

walk(data)
PY
}

normalize_minio_target() {
  local spec="$1"
  local bucket prefix

  spec="${spec#"${spec%%[![:space:]]*}"}"
  spec="${spec%"${spec##*[![:space:]]}"}"
  spec="${spec#"$MINIO_ALIAS/"}"
  spec="${spec#s3/}"
  spec="${spec#/}"
  bucket="${spec%%/*}"

  if [[ "$bucket" == "$spec" ]]; then
    die "MinIO target must be BUCKET/PREFIX, got: $1"
  fi

  prefix="${spec#*/}"
  prefix="${prefix#/}"
  prefix="${prefix%/}/"

  [[ -n "$bucket" ]] || die "MinIO target bucket must not be empty: $1"
  [[ -n "$prefix" && "$prefix" != "/" ]] || die "MinIO target prefix must not be empty: $1"
  if [[ "$bucket" == *"*"* || "$bucket" == *"?"* || "$prefix" == *"*"* || "$prefix" == *"?"* ]]; then
    die "MinIO target must not contain wildcards: $1"
  fi

  printf '%s/%s\n' "$bucket" "$prefix"
}

build_minio_targets() {
  local output target raw_target
  MINIO_TARGET_LIST=()

  if ((${#MINIO_TARGET_ARGS[@]} > 0)); then
    for raw_target in "${MINIO_TARGET_ARGS[@]}"; do
      target="$(normalize_minio_target "$raw_target")"
      MINIO_TARGET_LIST+=("$target")
    done
  elif [[ -n "${MINIO_TARGETS:-}" ]]; then
    local old_ifs="$IFS"
    IFS=','
    read -r -a MINIO_TARGET_LIST <<< "$MINIO_TARGETS"
    IFS="$old_ifs"
    for i in "${!MINIO_TARGET_LIST[@]}"; do
      MINIO_TARGET_LIST[$i]="$(normalize_minio_target "${MINIO_TARGET_LIST[$i]}")"
    done
  else
    output="$(collect_config_minio_targets)"
    while IFS= read -r target; do
      [[ -n "$target" ]] || continue
      MINIO_TARGET_LIST+=("$(normalize_minio_target "$target")")
    done <<< "$output"
  fi
}

cleanup_minio_target() {
  local target="$1"
  local bucket="${target%%/*}"
  local prefix="${target#*/}"
  local uri="$MINIO_ALIAS/$bucket/$prefix"
  local objects

  if ! run_mc ls "$MINIO_ALIAS/$bucket" >/dev/null 2>&1; then
    log "MinIO bucket not found, skipping: $bucket"
    return
  fi

  if (( DRY_RUN == 1 )); then
    log "MinIO dry run: would delete objects under $bucket/$prefix"
    objects="$(run_mc ls --recursive "$uri" 2>/dev/null || true)"
    if [[ -z "$objects" ]]; then
      log "  no objects found"
      return
    fi
    while IFS= read -r line; do
      log "  $line"
    done <<< "$objects"
  else
    log "Deleting MinIO objects under $bucket/$prefix"
    run_mc rm --recursive --force "$uri"
  fi
}

cleanup_minio() {
  build_minio_targets
  if ((${#MINIO_TARGET_LIST[@]} == 0)); then
    log "No MinIO Files prefixes found in $CONFIG_PATH; skipping MinIO cleanup."
    return
  fi

  setup_minio_alias

  local target
  for target in "${MINIO_TARGET_LIST[@]}"; do
    cleanup_minio_target "$target"
  done
}

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
UTILS_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
WORKSPACE_ROOT="$(cd -- "$UTILS_ROOT/.." && pwd)"

ORIG_ROOT_INPUT="${ORIG_ROOT:-$WORKSPACE_ROOT/self-service-asap}"
CONFIG_PATH_INPUT="${CONFIG_PATH:-}"
INDEX_PREFIX="${INDEX_PREFIX:-rag-tool-asap}"
MINIO_ALIAS="${MINIO_ALIAS:-minio}"
DOCKER_NETWORK="${DOCKER_NETWORK:-self_service}"
CURL_IMAGE="${CURL_IMAGE:-curlimages/curl:8.10.1}"
MINIO_IMAGE="${MINIO_IMAGE:-minio/mc:latest}"
USE_DOCKER_CURL="${USE_DOCKER_CURL:-1}"
DRY_RUN=1
CLEAN_OPENSEARCH=1
CLEAN_MINIO=1
DELETE_OPENSEARCH_INDICES=0
MINIO_ALIAS_READY=0
DOC_HASH_ARGS=()
DOC_HASH_LIST=()
MINIO_TARGET_ARGS=()
MINIO_TARGET_LIST=()

while (($#)); do
  case "$1" in
    --yes)
      DRY_RUN=0
      ;;
    --dry-run)
      DRY_RUN=1
      ;;
    --only)
      require_value "$1" "${2:-}"
      CLEAN_OPENSEARCH=0
      CLEAN_MINIO=0
      case "$2" in
        opensearch)
          CLEAN_OPENSEARCH=1
          ;;
        minio)
          CLEAN_MINIO=1
          ;;
        *)
          die "--only must be either opensearch or minio."
          ;;
      esac
      shift
      ;;
    --skip-opensearch)
      CLEAN_OPENSEARCH=0
      ;;
    --skip-minio)
      CLEAN_MINIO=0
      ;;
    --orig-root)
      require_value "$1" "${2:-}"
      ORIG_ROOT_INPUT="$2"
      shift
      ;;
    --config)
      require_value "$1" "${2:-}"
      CONFIG_PATH_INPUT="$2"
      shift
      ;;
    --index-prefix)
      require_value "$1" "${2:-}"
      INDEX_PREFIX="$2"
      shift
      ;;
    --delete-opensearch-indices)
      DELETE_OPENSEARCH_INDICES=1
      ;;
    --doc-hash)
      require_value "$1" "${2:-}"
      DOC_HASH_ARGS+=("$2")
      shift
      ;;
    --opensearch-url)
      require_value "$1" "${2:-}"
      OPENSEARCH_URL="$2"
      shift
      ;;
    --host-curl)
      USE_DOCKER_CURL=0
      ;;
    --minio-endpoint)
      require_value "$1" "${2:-}"
      MINIO_ENDPOINT="$2"
      shift
      ;;
    --minio-target)
      require_value "$1" "${2:-}"
      MINIO_TARGET_ARGS+=("$2")
      shift
      ;;
    --docker-network)
      require_value "$1" "${2:-}"
      DOCKER_NETWORK="$2"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "Unknown option: $1"
      ;;
  esac
  shift
done

[[ -d "$ORIG_ROOT_INPUT" ]] || die "self-service-asap was not found at $ORIG_ROOT_INPUT"
ORIG_ROOT="$(cd -- "$ORIG_ROOT_INPUT" && pwd)"
CONFIG_PATH="${CONFIG_PATH_INPUT:-$ORIG_ROOT/services/components/rag_tool_asap/config.json}"

load_env_file_preserving_overrides "$ORIG_ROOT/secrets/minio.env"
load_env_file_preserving_overrides "$ORIG_ROOT/secrets/opensearch.env"

OPENSEARCH_HOST="${OPENSEARCH_HOST:-opensearch}"
OPENSEARCH_PORT="${OPENSEARCH_PORT:-9200}"
OPENSEARCH_URL="${OPENSEARCH_URL:-http://${OPENSEARCH_HOST}:${OPENSEARCH_PORT}}"
OPENSEARCH_URL="$(with_http_scheme "$OPENSEARCH_URL")"

MINIO_ENDPOINT="${MINIO_ENDPOINT:-${MINIO_URL:-minio-storage:9000}}"
MINIO_ENDPOINT_URL="$(with_http_scheme "$MINIO_ENDPOINT")"
MINIO_ACCESS_KEY="${MINIO_ACCESS_KEY:-my_login}"
MINIO_SECRET_KEY="${MINIO_SECRET_KEY:-my_password}"
MINIO_MC_DIR="${MINIO_MC_DIR:-$ORIG_ROOT/.mc}"

NEEDS_MINIO_CLIENT=0
if (( CLEAN_MINIO == 1 || (CLEAN_OPENSEARCH == 1 && DELETE_OPENSEARCH_INDICES == 0 && ${#DOC_HASH_ARGS[@]} == 0) )); then
  NEEDS_MINIO_CLIENT=1
fi

if (( CLEAN_OPENSEARCH == 1 && DELETE_OPENSEARCH_INDICES == 0 )); then
  command -v python3 >/dev/null || die "python3 is required for OpenSearch doc-hash cleanup."
fi
if (( CLEAN_MINIO == 1 && ${#MINIO_TARGET_ARGS[@]} == 0 )) && [[ -z "${MINIO_TARGETS:-}" ]]; then
  command -v python3 >/dev/null || die "python3 is required to parse $CONFIG_PATH."
fi
if (( CLEAN_OPENSEARCH == 1 && USE_DOCKER_CURL == 0 )); then
  command -v curl >/dev/null || die "curl is required when --host-curl is used."
fi
if (( NEEDS_MINIO_CLIENT == 1 || (CLEAN_OPENSEARCH == 1 && USE_DOCKER_CURL == 1) )); then
  command -v docker >/dev/null || die "docker is required for Docker-based cleanup clients."
fi

log "Mode: $([[ "$DRY_RUN" == 1 ]] && printf 'dry run' || printf 'delete')"
log "Config: $CONFIG_PATH"

if (( CLEAN_OPENSEARCH == 1 )); then
  log "OpenSearch: $OPENSEARCH_URL, index prefix: $INDEX_PREFIX"
  if (( DELETE_OPENSEARCH_INDICES == 1 )); then
    cleanup_opensearch_indices
  else
    cleanup_opensearch_doc_hashes
  fi
else
  log "Skipping OpenSearch cleanup."
fi

if (( CLEAN_MINIO == 1 )); then
  log "MinIO: $MINIO_ENDPOINT_URL"
  cleanup_minio
else
  log "Skipping MinIO cleanup."
fi

if (( DRY_RUN == 1 )); then
  log "Dry run finished. Re-run with --yes to delete these resources."
else
  log "Cleanup finished."
fi
