#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/cleanup-rag-tool-asap-containers.sh [options]

Stop and remove Docker containers used by rag_tool_asap evaluation.

By default this is a dry run. Pass --yes to stop and remove containers.
All targets are selected by default.

Targets:
  rag-tool-asap   Component container for compose service base_rag_tool_asap.
  minio           MinIO container for compose service minio-storage.
  opensearch      OpenSearch container for compose service opensearch.

Options:
  --yes                         Stop and remove the selected containers.
  --dry-run                     Show what would be removed. This is the default.
  --volumes, --remove-volumes   Also remove named volumes used by selected targets.
  --only TARGET[,TARGET...]     Clean only selected target(s). Can be repeated.
  --skip-rag-tool-asap          Do not remove the rag_tool_asap component container.
  --skip-component              Alias for --skip-rag-tool-asap.
  --skip-minio                  Do not remove the MinIO container.
  --skip-opensearch             Do not remove the OpenSearch container.
  --orig-root PATH              Path to self-service-asap.
  -h, --help                    Show this help.

Environment overrides:
  ORIG_ROOT, INFRA_PROJECT_NAME, COMPONENT_PROJECT_NAME,
  RAG_TOOL_ASAP_SERVICE, MINIO_SERVICE, OPENSEARCH_SERVICE,
  RAG_TOOL_ASAP_VOLUMES, MINIO_VOLUMES, OPENSEARCH_VOLUMES.

Examples:
  scripts/cleanup-rag-tool-asap-containers.sh
  scripts/cleanup-rag-tool-asap-containers.sh --yes
  scripts/cleanup-rag-tool-asap-containers.sh --yes --only rag-tool-asap
  scripts/cleanup-rag-tool-asap-containers.sh --yes --skip-minio --volumes
EOF
}

log() {
  printf '[asap-eval:containers] %s\n' "$*"
}

die() {
  printf '[asap-eval:containers] ERROR: %s\n' "$*" >&2
  exit 1
}

require_value() {
  local option="$1"
  local value="${2:-}"

  if [[ -z "$value" ]]; then
    die "$option requires a value."
  fi
}

trim() {
  local value="$1"

  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s\n' "$value"
}

normalize_target() {
  local target="$1"

  target="$(trim "$target")"
  case "$target" in
    rag-tool-asap|rag_tool_asap|base_rag_tool_asap|component)
      printf 'rag-tool-asap\n'
      ;;
    minio|minio-storage|minio_storage)
      printf 'minio\n'
      ;;
    opensearch|open-search|open_search)
      printf 'opensearch\n'
      ;;
    *)
      die "unknown target: $target"
      ;;
  esac
}

disable_all_targets() {
  CLEAN_RAG_TOOL_ASAP=0
  CLEAN_MINIO=0
  CLEAN_OPENSEARCH=0
}

enable_target() {
  local target
  target="$(normalize_target "$1")"

  case "$target" in
    rag-tool-asap)
      CLEAN_RAG_TOOL_ASAP=1
      ;;
    minio)
      CLEAN_MINIO=1
      ;;
    opensearch)
      CLEAN_OPENSEARCH=1
      ;;
  esac
}

enable_only_targets() {
  local value="$1"
  local target

  if (( ONLY_TARGETS_SEEN == 0 )); then
    disable_all_targets
    ONLY_TARGETS_SEEN=1
  fi

  IFS=',' read -r -a targets <<< "$value"
  for target in "${targets[@]}"; do
    target="$(trim "$target")"
    require_value "--only" "$target"
    enable_target "$target"
  done
}

skip_target() {
  local target
  target="$(normalize_target "$1")"

  case "$target" in
    rag-tool-asap)
      CLEAN_RAG_TOOL_ASAP=0
      ;;
    minio)
      CLEAN_MINIO=0
      ;;
    opensearch)
      CLEAN_OPENSEARCH=0
      ;;
  esac
}

selected_target_count() {
  printf '%s\n' "$((CLEAN_RAG_TOOL_ASAP + CLEAN_MINIO + CLEAN_OPENSEARCH))"
}

add_unique_volume() {
  local volume="$1"
  local existing

  [[ -n "$volume" ]] || return 0
  for existing in "${VOLUMES_TO_REMOVE[@]}"; do
    if [[ "$existing" == "$volume" ]]; then
      return 0
    fi
  done
  VOLUMES_TO_REMOVE+=("$volume")
}

add_volume_list() {
  local values="$1"
  local volume

  [[ -n "$values" ]] || return 0
  IFS=',' read -r -a volumes <<< "$values"
  for volume in "${volumes[@]}"; do
    volume="$(trim "$volume")"
    add_unique_volume "$volume"
  done
}

compose_container_ids() {
  local project_name="$1"
  local service_name="$2"

  docker ps -a \
    --filter "label=com.docker.compose.project=$project_name" \
    --filter "label=com.docker.compose.service=$service_name" \
    --format '{{.ID}}'
}

compose_container_summary() {
  local project_name="$1"
  local service_name="$2"

  docker ps -a \
    --filter "label=com.docker.compose.project=$project_name" \
    --filter "label=com.docker.compose.service=$service_name" \
    --format '{{.Names}} [{{.Status}}]'
}

collect_container_volumes() {
  local container_id
  local volume

  for container_id in "$@"; do
    while IFS= read -r volume; do
      add_unique_volume "$volume"
    done < <(
      docker inspect \
        --format '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}}{{"\n"}}{{end}}{{end}}' \
        "$container_id" 2>/dev/null || true
    )
  done
}

collect_target_volumes() {
  local project_name="$1"
  local service_name="$2"
  local configured_volumes="$3"
  local container_ids=()
  local container_id

  add_volume_list "$configured_volumes"

  while IFS= read -r container_id; do
    [[ -n "$container_id" ]] || continue
    container_ids+=("$container_id")
  done < <(compose_container_ids "$project_name" "$service_name")

  if ((${#container_ids[@]} > 0)); then
    collect_container_volumes "${container_ids[@]}"
  fi
}

remove_target_containers() {
  local target_name="$1"
  local project_name="$2"
  local service_name="$3"
  local container_ids=()
  local container_id
  local summaries=()
  local summary

  while IFS= read -r container_id; do
    [[ -n "$container_id" ]] || continue
    container_ids+=("$container_id")
  done < <(compose_container_ids "$project_name" "$service_name")

  while IFS= read -r summary; do
    [[ -n "$summary" ]] || continue
    summaries+=("$summary")
  done < <(compose_container_summary "$project_name" "$service_name")

  if ((${#container_ids[@]} == 0)); then
    log "$target_name: no containers found for compose project=$project_name service=$service_name."
    return 0
  fi

  if (( DRY_RUN == 1 )); then
    log "$target_name dry run: would stop and remove ${#container_ids[@]} container(s): ${summaries[*]}"
    return 0
  fi

  log "$target_name: stopping and removing ${#container_ids[@]} container(s): ${summaries[*]}"
  docker rm --force "${container_ids[@]}" >/dev/null
}

remove_selected_volumes() {
  local volume
  local failed=0

  if ((${#VOLUMES_TO_REMOVE[@]} == 0)); then
    log "No named volumes selected for removal."
    return 0
  fi

  for volume in "${VOLUMES_TO_REMOVE[@]}"; do
    if ! docker volume inspect "$volume" >/dev/null 2>&1; then
      log "Volume not found, skipping: $volume"
      continue
    fi

    if (( DRY_RUN == 1 )); then
      log "Volume dry run: would remove $volume"
      continue
    fi

    log "Removing Docker volume: $volume"
    if ! docker volume rm "$volume" >/dev/null; then
      log "ERROR: failed to remove volume $volume. It may still be used by another container."
      failed=1
    fi
  done

  return "$failed"
}

DRY_RUN=1
REMOVE_VOLUMES=0
ONLY_TARGETS_SEEN=0
CLEAN_RAG_TOOL_ASAP=1
CLEAN_MINIO=1
CLEAN_OPENSEARCH=1
ORIG_ROOT_ARG=""

while (($#)); do
  case "$1" in
    --yes)
      DRY_RUN=0
      ;;
    --dry-run)
      DRY_RUN=1
      ;;
    --volumes|--remove-volumes)
      REMOVE_VOLUMES=1
      ;;
    --only)
      shift
      require_value "--only" "${1:-}"
      enable_only_targets "$1"
      ;;
    --only=*)
      only_value="${1#*=}"
      require_value "--only" "$only_value"
      enable_only_targets "$only_value"
      ;;
    --skip-rag-tool-asap|--skip-component)
      skip_target rag-tool-asap
      ;;
    --skip-minio)
      skip_target minio
      ;;
    --skip-opensearch)
      skip_target opensearch
      ;;
    --orig-root)
      shift
      require_value "--orig-root" "${1:-}"
      ORIG_ROOT_ARG="$1"
      ;;
    --orig-root=*)
      ORIG_ROOT_ARG="${1#*=}"
      require_value "--orig-root" "$ORIG_ROOT_ARG"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
  shift
done

if [[ "$(selected_target_count)" == "0" ]]; then
  die "no cleanup targets selected."
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
EVAL_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
ORIG_ROOT="${ORIG_ROOT_ARG:-${ORIG_ROOT:-$EVAL_ROOT/../self-service-asap}}"

if [[ ! -d "$ORIG_ROOT" ]]; then
  die "self-service-asap was not found at $ORIG_ROOT"
fi

ORIG_ROOT="$(cd -- "$ORIG_ROOT" && pwd)"
INFRA_PROJECT_NAME="${INFRA_PROJECT_NAME:-$(basename "$ORIG_ROOT")}"
COMPONENT_PROJECT_NAME="${COMPONENT_PROJECT_NAME:-$(basename "$ORIG_ROOT/scenarios")}"
RAG_TOOL_ASAP_SERVICE="${RAG_TOOL_ASAP_SERVICE:-base_rag_tool_asap}"
MINIO_SERVICE="${MINIO_SERVICE:-minio-storage}"
OPENSEARCH_SERVICE="${OPENSEARCH_SERVICE:-opensearch}"
RAG_TOOL_ASAP_VOLUMES="${RAG_TOOL_ASAP_VOLUMES:-}"
MINIO_VOLUMES="${MINIO_VOLUMES:-${INFRA_PROJECT_NAME}_minio_data}"
OPENSEARCH_VOLUMES="${OPENSEARCH_VOLUMES:-${INFRA_PROJECT_NAME}_opensearch-data}"
VOLUMES_TO_REMOVE=()

command -v docker >/dev/null || die "docker is required."

log "Evaluation root: $EVAL_ROOT"
log "self-service-asap root: $ORIG_ROOT"
log "Mode: $([[ "$DRY_RUN" == "1" ]] && printf 'dry-run' || printf 'delete')"

if (( REMOVE_VOLUMES == 1 )); then
  log "Volume removal is enabled for selected targets."
  if (( CLEAN_RAG_TOOL_ASAP == 1 )); then
    collect_target_volumes "$COMPONENT_PROJECT_NAME" "$RAG_TOOL_ASAP_SERVICE" "$RAG_TOOL_ASAP_VOLUMES"
  fi
  if (( CLEAN_MINIO == 1 )); then
    collect_target_volumes "$INFRA_PROJECT_NAME" "$MINIO_SERVICE" "$MINIO_VOLUMES"
  fi
  if (( CLEAN_OPENSEARCH == 1 )); then
    collect_target_volumes "$INFRA_PROJECT_NAME" "$OPENSEARCH_SERVICE" "$OPENSEARCH_VOLUMES"
  fi
else
  log "Volume removal is disabled. Pass --volumes to remove selected target volumes."
fi

if (( CLEAN_RAG_TOOL_ASAP == 1 )); then
  remove_target_containers rag-tool-asap "$COMPONENT_PROJECT_NAME" "$RAG_TOOL_ASAP_SERVICE"
fi
if (( CLEAN_MINIO == 1 )); then
  remove_target_containers minio "$INFRA_PROJECT_NAME" "$MINIO_SERVICE"
fi
if (( CLEAN_OPENSEARCH == 1 )); then
  remove_target_containers opensearch "$INFRA_PROJECT_NAME" "$OPENSEARCH_SERVICE"
fi

if (( REMOVE_VOLUMES == 1 )); then
  remove_selected_volumes
fi

log "Cleanup finished."
