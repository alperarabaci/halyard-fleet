#!/usr/bin/env bash
#
# Safely apply the hook configuration required by the multi-runtime routing
# fixes. This script records exact pre-transition copies before invoking
# `halyard wire`, which performs its own merge and timestamped backup.
#
# Usage:
#   scripts/rollout-runtime-routing-fixes.sh /path/to/project [...]
#
# The script does not restart Halyard, Claude Desktop, or Codex. It prints the
# required manual steps when the hook transition is complete.

set -euo pipefail

if [[ "$#" -eq 0 ]]; then
  echo "usage: $0 /path/to/project [...]" >&2
  exit 64
fi

HALYARD_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HALYARD_TRANSITION_ROOT="${HALYARD_TRANSITION_STATE_ROOT:-${XDG_STATE_HOME:-${HOME}/.local/state}/halyard-fleet/transitions}"
HALYARD_TRANSITION_ID="$(date -u +%Y%m%dT%H%M%SZ)"
HALYARD_STATE_DIR="${HALYARD_TRANSITION_ROOT}/${HALYARD_TRANSITION_ID}"

if [[ -e "${HALYARD_STATE_DIR}" ]]; then
  HALYARD_STATE_DIR="${HALYARD_STATE_DIR}-${RANDOM}"
fi

mkdir -p "${HALYARD_STATE_DIR}/projects"
chmod 700 "${HALYARD_STATE_DIR}" "${HALYARD_STATE_DIR}/projects"

{
  echo "format=1"
  echo "created_at=${HALYARD_TRANSITION_ID}"
  echo "halyard_root=${HALYARD_ROOT}"
} >"${HALYARD_STATE_DIR}/transition.env"
chmod 600 "${HALYARD_STATE_DIR}/transition.env"

snapshot_file() {
  local source_path="$1"
  local snapshot_path="$2"
  local status_path="$3"

  if [[ -f "${source_path}" ]]; then
    cp -p "${source_path}" "${snapshot_path}"
    echo "present" >"${status_path}"
  else
    echo "absent" >"${status_path}"
  fi
  chmod 600 "${status_path}"
}

project_index=0
for requested_project in "$@"; do
  project_index=$((project_index + 1))
  project_root="$(git -C "${requested_project}" rev-parse --show-toplevel)"
  project_state="${HALYARD_STATE_DIR}/projects/$(printf '%03d' "${project_index}")"

  mkdir -p "${project_state}"
  chmod 700 "${project_state}"
  printf '%s\n' "${project_root}" >"${project_state}/project.path"
  chmod 600 "${project_state}/project.path"

  snapshot_file \
    "${project_root}/.claude/settings.local.json" \
    "${project_state}/claude-settings.local.json.before" \
    "${project_state}/claude.status"
  snapshot_file \
    "${project_root}/.codex/hooks.json" \
    "${project_state}/codex-hooks.json.before" \
    "${project_state}/codex.status"

  echo "Wiring ${project_root}"
  (
    cd "${HALYARD_ROOT}"
    uv run halyard wire "${project_root}"
  )

  if [[ -f "${project_root}/.claude/settings.local.json" ]]; then
    cp -p \
      "${project_root}/.claude/settings.local.json" \
      "${project_state}/claude-settings.local.json.after"
  fi
  if [[ -f "${project_root}/.codex/hooks.json" ]]; then
    cp -p \
      "${project_root}/.codex/hooks.json" \
      "${project_state}/codex-hooks.json.after"
  fi
done

cat <<EOF

Hook transition complete.

Recovery state:
  ${HALYARD_STATE_DIR}

Next steps:
  1. Restart the Halyard control plane.
  2. Restart or reopen affected Claude Code and Codex tasks so they reload hooks.
  3. Review and trust newly added Codex hooks when Codex asks.
  4. Run: cd "${HALYARD_ROOT}" && uv run halyard doctor
  5. Keep a Claude Desktop task open and send one message from its Telegram group.

Rollback command:
  "${HALYARD_ROOT}/scripts/rollback-runtime-routing-fixes.sh" "${HALYARD_STATE_DIR}"
EOF
