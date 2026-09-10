#!/usr/bin/env bash
set -euo pipefail

# 将工作区中的 custom-skills + zhenyun-pangu-mcp 同步到个人 Codex 插件，
# 更新 cachebuster 后重新安装，使新线程自动加载最新 skills/MCP。
MCP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE_ROOT="$(cd "${MCP_ROOT}/.." && pwd)"
PLUGIN_ROOT="${PLUGIN_ROOT:-/Users/chuanni/plugins/zhenyun-pangu-toolkit}"
PLUGIN_CREATOR_ROOT="/Users/chuanni/.codex/skills/.system/plugin-creator"
SKILL_CREATOR_ROOT="/Users/chuanni/.codex/skills/.system/skill-creator"
UV_CACHE_DIR_VALUE="${UV_CACHE_DIR:-/private/tmp/zhenyun-uv-cache}"

MARKETPLACE_NAME="$(
  UV_CACHE_DIR="${UV_CACHE_DIR_VALUE}" \
    uv run --no-project python "${PLUGIN_CREATOR_ROOT}/scripts/read_marketplace_name.py"
)"

for skill_dir in "${WORKSPACE_ROOT}"/custom-skills/*; do
  [[ -d "${skill_dir}" ]] || continue
  UV_CACHE_DIR="${UV_CACHE_DIR_VALUE}" \
    uv run --no-project --with pyyaml python \
    "${SKILL_CREATOR_ROOT}/scripts/quick_validate.py" "${skill_dir}"
done

(
  cd "${MCP_ROOT}"
  UV_CACHE_DIR="${UV_CACHE_DIR_VALUE}" uv run pytest -q
  UV_CACHE_DIR="${UV_CACHE_DIR_VALUE}" uv run python scripts/validate_skill_mcp_contracts.py
)

mkdir -p "${PLUGIN_ROOT}/skills" "${PLUGIN_ROOT}/servers/zhenyun-pangu-mcp"
rsync -a --delete --exclude='.DS_Store' --exclude='.git' \
  "${WORKSPACE_ROOT}/custom-skills/" "${PLUGIN_ROOT}/skills/"
rsync -a --delete --exclude='.DS_Store' --exclude='.git' --exclude='.env' \
  --exclude='.venv' --exclude='.pytest_cache' --exclude='__pycache__' \
  --exclude='*.pyc' --exclude='dist' --exclude='build' --exclude='.mcp.json' \
  "${MCP_ROOT}/" "${PLUGIN_ROOT}/servers/zhenyun-pangu-mcp/"

UV_CACHE_DIR="${UV_CACHE_DIR_VALUE}" \
  uv run --no-project --with pyyaml python "${PLUGIN_CREATOR_ROOT}/scripts/validate_plugin.py" \
  "${PLUGIN_ROOT}"
UV_CACHE_DIR="${UV_CACHE_DIR_VALUE}" \
  uv run --no-project python "${PLUGIN_CREATOR_ROOT}/scripts/update_plugin_cachebuster.py" \
  "${PLUGIN_ROOT}"
codex plugin add "zhenyun-pangu-toolkit@${MARKETPLACE_NAME}"

PLUGIN_VERSION="$(
  sed -n 's/^[[:space:]]*"version":[[:space:]]*"\([^"]*\)".*/\1/p' \
    "${PLUGIN_ROOT}/.codex-plugin/plugin.json" | head -n 1
)"
INSTALLED_PLUGIN_ROOT="/Users/chuanni/.codex/plugins/cache/${MARKETPLACE_NAME}/zhenyun-pangu-toolkit/${PLUGIN_VERSION}"
if [[ -z "${PLUGIN_VERSION}" || ! -d "${INSTALLED_PLUGIN_ROOT}" ]]; then
  echo "Installed plugin cache not found for version ${PLUGIN_VERSION:-<empty>}." >&2
  exit 1
fi

skill_drift="$(
  rsync -ani --delete --exclude='.DS_Store' --exclude='.git' \
    "${WORKSPACE_ROOT}/custom-skills/" "${PLUGIN_ROOT}/skills/"
)"
mcp_drift="$(
  rsync -ani --delete --exclude='.DS_Store' --exclude='.git' --exclude='.env' \
    --exclude='.venv' --exclude='.pytest_cache' --exclude='__pycache__' \
    --exclude='*.pyc' --exclude='dist' --exclude='build' --exclude='.mcp.json' \
    "${MCP_ROOT}/" "${PLUGIN_ROOT}/servers/zhenyun-pangu-mcp/"
)"
cache_drift="$(
  rsync -acni --delete --exclude='.DS_Store' --exclude='.git' --exclude='.env' \
    --exclude='.venv' --exclude='.pytest_cache' --exclude='__pycache__' \
    --exclude='*.pyc' --exclude='dist' --exclude='build' \
    --exclude='/servers/zhenyun-pangu-mcp/.mcp.json' \
    "${PLUGIN_ROOT}/" "${INSTALLED_PLUGIN_ROOT}/" | awk '$1 !~ /^\.d/'
)"
if [[ -n "${skill_drift}" || -n "${mcp_drift}" || -n "${cache_drift}" ]]; then
  echo "Plugin source drift detected after synchronization." >&2
  [[ -n "${skill_drift}" ]] && echo "${skill_drift}" >&2
  [[ -n "${mcp_drift}" ]] && echo "${mcp_drift}" >&2
  [[ -n "${cache_drift}" ]] && echo "${cache_drift}" >&2
  exit 1
fi

echo "Updated zhenyun-pangu-toolkit from ${WORKSPACE_ROOT}"
echo "Verified skill/MCP contracts and workspace-to-source-to-cache synchronization."
echo "Start a new Codex task to load the updated plugin."
