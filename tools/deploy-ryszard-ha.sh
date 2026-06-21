#!/usr/bin/env bash
set -euo pipefail

CALLER_CWD="$PWD"
cd "$(dirname "$0")/.."

SOURCE_DIR="custom_components/ryszard"
REMOTE="root@homeassistant.local"
REMOTE_DIR="/config/custom_components/ryszard"
DRY_RUN=0
CHECK=0
RESTART=0

usage() {
  cat <<'USAGE'
Usage: tools/deploy-ryszard-ha.sh [options]

Upload the Ryszard Home Assistant custom integration to a Home Assistant host.

Options:
  --host USER@HOST      SSH destination. Default: root@homeassistant.local
  --remote-dir PATH     Remote integration directory.
                        Default: /config/custom_components/ryszard
  --dry-run             Show what would be uploaded without changing HA files.
  --check               Run `ha core check` after upload.
  --restart             Run `ha core check` and then `ha core restart`.
  -h, --help            Show this help.
USAGE
}

die() {
  echo "deploy-ryszard-ha.sh: $1" >&2
  usage >&2
  exit 2
}

resolve_path() {
  if [[ "$1" = /* ]]; then
    printf '%s\n' "$1"
  else
    printf '%s/%s\n' "$CALLER_CWD" "$1"
  fi
}

sh_quote() {
  printf "'%s'" "${1//\'/\'\\\'\'}"
}

validate_remote_dir() {
  if [[ "$REMOTE_DIR" != /* ]]; then
    die "--remote-dir must be an absolute path"
  fi
  if [[ "$(basename "$REMOTE_DIR")" != "ryszard" ]]; then
    die "--remote-dir must end with /ryszard"
  fi
}

ssh_run() {
  ssh "$REMOTE" "$1"
}

rsync_upload() {
  local dry_run_arg=()
  if [[ "$DRY_RUN" -eq 1 ]]; then
    dry_run_arg=(--dry-run --itemize-changes)
  fi

  if [[ "$DRY_RUN" -eq 0 ]]; then
    ssh_run "mkdir -p $(sh_quote "$REMOTE_DIR")"
  fi
  rsync \
    -az \
    --delete \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='.DS_Store' \
    "${dry_run_arg[@]}" \
    "$SOURCE_DIR"/ \
    "$REMOTE:$REMOTE_DIR"/
}

remote_has_rsync() {
  ssh_run "command -v rsync >/dev/null 2>&1"
}

tar_upload() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "rsync is not available on both ends; tar fallback would replace $REMOTE:$REMOTE_DIR"
    return
  fi

  local remote_parent
  local remote_tmp
  remote_parent="$(dirname "$REMOTE_DIR")"
  remote_tmp="${REMOTE_DIR}.tmp.$$"

  ssh_run "rm -rf $(sh_quote "$remote_tmp") && mkdir -p $(sh_quote "$remote_tmp") $(sh_quote "$remote_parent")"
  tar \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.DS_Store' \
    -C "$SOURCE_DIR" \
    -czf - \
    . | ssh "$REMOTE" "tar -xzf - -C $(sh_quote "$remote_tmp") && rm -rf $(sh_quote "$REMOTE_DIR") && mv $(sh_quote "$remote_tmp") $(sh_quote "$REMOTE_DIR")"
}

run_ha_check() {
  echo "Running Home Assistant config check..."
  ssh_run "ha core check"
}

run_ha_restart() {
  echo "Restarting Home Assistant core..."
  ssh_run "ha core restart"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      if [[ $# -lt 2 || -z "$2" ]]; then
        die "--host requires USER@HOST"
      fi
      REMOTE="$2"
      shift 2
      ;;
    --host=*)
      if [[ -z "${1#--host=}" ]]; then
        die "--host requires USER@HOST"
      fi
      REMOTE="${1#--host=}"
      shift
      ;;
    --remote-dir)
      if [[ $# -lt 2 || -z "$2" ]]; then
        die "--remote-dir requires a path"
      fi
      REMOTE_DIR="$2"
      shift 2
      ;;
    --remote-dir=*)
      if [[ -z "${1#--remote-dir=}" ]]; then
        die "--remote-dir requires a path"
      fi
      REMOTE_DIR="${1#--remote-dir=}"
      shift
      ;;
    --source-dir)
      if [[ $# -lt 2 || -z "$2" ]]; then
        die "--source-dir requires a path"
      fi
      SOURCE_DIR="$(resolve_path "$2")"
      shift 2
      ;;
    --source-dir=*)
      if [[ -z "${1#--source-dir=}" ]]; then
        die "--source-dir requires a path"
      fi
      SOURCE_DIR="$(resolve_path "${1#--source-dir=}")"
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --check)
      CHECK=1
      shift
      ;;
    --restart)
      CHECK=1
      RESTART=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
done

validate_remote_dir

if [[ ! -d "$SOURCE_DIR" ]]; then
  echo "Source integration directory does not exist: $SOURCE_DIR" >&2
  exit 1
fi

echo "Uploading $SOURCE_DIR to $REMOTE:$REMOTE_DIR"
if command -v rsync >/dev/null 2>&1 && remote_has_rsync; then
  rsync_upload
else
  tar_upload
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "Dry run complete. No files were changed."
  exit 0
fi

echo "Upload complete."

if [[ "$CHECK" -eq 1 ]]; then
  run_ha_check
fi

if [[ "$RESTART" -eq 1 ]]; then
  run_ha_restart
fi
