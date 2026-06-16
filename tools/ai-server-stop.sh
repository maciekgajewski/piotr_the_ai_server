#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 0 ]]; then
  echo "Usage: tools/ai-server-stop.sh" >&2
  exit 2
fi

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd -P)"
PROJECT_NAME="$(basename "$REPO_ROOT")"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required." >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "Cannot access the Docker daemon." >&2
  exit 1
fi

label_value() {
  local container_id="$1"
  local label_name="$2"
  docker inspect --format "{{ index .Config.Labels \"$label_name\" }}" "$container_id"
}

is_this_project_container() {
  local container_id="$1"
  local working_dir
  local config_files
  local project

  working_dir="$(label_value "$container_id" "com.docker.compose.project.working_dir")"
  config_files="$(label_value "$container_id" "com.docker.compose.project.config_files")"
  project="$(label_value "$container_id" "com.docker.compose.project")"

  if [[ "$working_dir" == "$REPO_ROOT" ]]; then
    return 0
  fi

  if [[ "$config_files" == *"$REPO_ROOT/docker-compose.ai-server.yml"* ]]; then
    return 0
  fi

  if [[ "$working_dir" == "<no value>" && "$config_files" == "<no value>" && "$project" == "$PROJECT_NAME" ]]; then
    return 0
  fi

  return 1
}

if ! CANDIDATE_OUTPUT="$(docker ps -q --filter "label=com.docker.compose.service=ai-server")"; then
  echo "Failed to list Docker containers." >&2
  exit 1
fi

CANDIDATE_IDS=()
if [[ -n "$CANDIDATE_OUTPUT" ]]; then
  mapfile -t CANDIDATE_IDS <<< "$CANDIDATE_OUTPUT"
fi

CONTAINER_IDS=()
for container_id in "${CANDIDATE_IDS[@]}"; do
  if is_this_project_container "$container_id"; then
    CONTAINER_IDS+=("$container_id")
  fi
done

if [[ ${#CONTAINER_IDS[@]} -eq 0 ]]; then
  echo "No running AI server containers found."
  exit 0
fi

docker stop "${CONTAINER_IDS[@]}"
