#!/usr/bin/env bash
set -euo pipefail

# 将工作区中的 custom-skills + zhenyun-pangu-mcp 同步到个人 Codex 插件，
# 更新 cachebuster 后重新安装，使新线程自动加载最新 skills/MCP。
MCP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE_ROOT="$(cd "${MCP_ROOT}/.." && pwd)"
PLUGIN_ROOT="${PLUGIN_ROOT:-/Users/chuanni/plugins/zhenyun-pangu-toolkit}"
PLUGIN_CREATOR_ROOT="/Users/chuanni/.codex/skills/.system/plugin-creator"

mkdir -p "${PLUGIN_ROOT}/skills" "${PLUGIN_ROOT}/servers/zhenyun-pangu-mcp"
rsync -a --delete --exclude='.DS_Store' --exclude='.git' \
  "${WORKSPACE_ROOT}/custom-skills/" "${PLUGIN_ROOT}/skills/"
rsync -a --delete --exclude='.DS_Store' --exclude='.git' --exclude='.env' \
  --exclude='.venv' --exclude='.pytest_cache' --exclude='__pycache__' \
  --exclude='*.pyc' --exclude='dist' --exclude='build' --exclude='.mcp.json' \
  "${MCP_ROOT}/" "${PLUGIN_ROOT}/servers/zhenyun-pangu-mcp/"

UV_CACHE_DIR="${UV_CACHE_DIR:-/private/tmp/zhenyun-uv-cache}" \
  uv run --no-project --with pyyaml python "${PLUGIN_CREATOR_ROOT}/scripts/validate_plugin.py" \
  "${PLUGIN_ROOT}"
UV_CACHE_DIR="${UV_CACHE_DIR:-/private/tmp/zhenyun-uv-cache}" \
  uv run --no-project python "${PLUGIN_CREATOR_ROOT}/scripts/update_plugin_cachebuster.py" \
  "${PLUGIN_ROOT}"
codex plugin add zhenyun-pangu-toolkit@personal

echo "Updated zhenyun-pangu-toolkit from ${WORKSPACE_ROOT}"
echo "Start a new Codex task to load the updated plugin."
