#!/usr/bin/env bash
#
# Restore the exact Claude and Codex project settings captured by
# rollout-runtime-routing-fixes.sh. Current post-transition files are retained
# inside the transition state directory before anything is restored.
#
# Usage:
#   scripts/rollback-runtime-routing-fixes.sh /path/to/transition-state

set -euo pipefail

if [[ "$#" -ne 1 ]]; then
  echo "usage: $0 /path/to/transition-state" >&2
  exit 64
fi

HALYARD_STATE_DIR="$1"
HALYARD_MANIFEST="${HALYARD_STATE_DIR}/transition.env"

if [[ ! -f "${HALYARD_MANIFEST}" ]] || ! grep -qx "format=1" "${HALYARD_MANIFEST}"; then
  echo "rollback refused: ${HALYARD_STATE_DIR} is not a supported transition state" >&2
  exit 65
fi

HALYARD_ROLLBACK_ID="$(date -u +%Y%m%dT%H%M%SZ)"

restore_file() {
  local status_path="$1"
  local snapshot_path="$2"
  local target_path="$3"
  local current_backup="$4"
  local status

  status="$(cat "${status_path}")"
  mkdir -p "$(dirname "${target_path}")"

  if [[ -f "${target_path}" ]]; then
    cp -p "${target_path}" "${current_backup}"
  fi

  case "${status}" in
    present)
      if [[ ! -f "${snapshot_path}" ]]; then
        echo "rollback refused: missing snapshot ${snapshot_path}" >&2
        exit 66
      fi
      cp -p "${snapshot_path}" "${target_path}"
      ;;
    absent)
      if [[ -f "${target_path}" ]]; then
        mv "${target_path}" "${current_backup}.removed"
      fi
      ;;
    *)
      echo "rollback refused: invalid status in ${status_path}" >&2
      exit 65
      ;;
  esac
}

found_projects=0
for project_state in "${HALYARD_STATE_DIR}"/projects/[0-9][0-9][0-9]; do
  if [[ ! -d "${project_state}" ]]; then
    continue
  fi
  found_projects=$((found_projects + 1))
  project_root="$(cat "${project_state}/project.path")"

  if [[ ! -d "${project_root}/.git" ]]; then
    echo "rollback refused: recorded project is no longer a git root: ${project_root}" >&2
    exit 65
  fi

  echo "Restoring ${project_root}"
  restore_file \
    "${project_state}/claude.status" \
    "${project_state}/claude-settings.local.json.before" \
    "${project_root}/.claude/settings.local.json" \
    "${project_state}/claude-settings.local.json.pre-rollback-${HALYARD_ROLLBACK_ID}"
  restore_file \
    "${project_state}/codex.status" \
    "${project_state}/codex-hooks.json.before" \
    "${project_root}/.codex/hooks.json" \
    "${project_state}/codex-hooks.json.pre-rollback-${HALYARD_ROLLBACK_ID}"
done

if [[ "${found_projects}" -eq 0 ]]; then
  echo "rollback refused: no project snapshots found in ${HALYARD_STATE_DIR}" >&2
  exit 65
fi

cat <<EOF

Project hook settings restored from:
  ${HALYARD_STATE_DIR}

The post-transition files were retained in that directory.

Next steps:
  1. Restart the Halyard control plane on the code version you intend to run.
  2. Restart or reopen affected Claude Code and Codex tasks so they reload hooks.
  3. Run the matching version's diagnostic command before resuming remote work.
EOF
