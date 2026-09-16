#!/usr/bin/env python3
"""Export an agent-neutral stdio MCP + skills bundle, without credentials/caches."""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

MCP_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = MCP_ROOT.parent / "custom-skills"


def _copy_sources(source: Path, target: Path, suffixes: set[str]) -> None:
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
            continue
        if path.suffix not in suffixes or not path.is_file():
            continue
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != source.parent):
            raise ValueError(f"Refusing symlink in bundle source: {relative}")
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)


def export_bundle(output: Path, env_dir: Path) -> dict:
    output = output.expanduser().resolve()
    env_dir = env_dir.expanduser().resolve()
    # Do not recurse into the output while enumerating sources; do not ship secrets.
    for source in (MCP_ROOT, SKILLS_ROOT):
        if output == source or source in output.parents:
            raise ValueError("Output must be outside MCP and skill source trees")
    if output == env_dir or output in env_dir.parents:
        raise ValueError("Credential directory must be outside the exported bundle")
    output.mkdir(parents=True, exist_ok=False)
    server_root = output / "server"
    server_root.mkdir()
    _copy_sources(MCP_ROOT / "src", server_root / "src", {".py", ".md"})
    _copy_sources(SKILLS_ROOT, output / "skills", {".md", ".json", ".yaml", ".yml", ".py", ".sql", ".js"})
    for name in ("pyproject.toml", "uv.lock", "README.md", "WORKFLOW_OPTIMIZATION.md", ".env.example"):
        source = MCP_ROOT / name
        if source.is_symlink():
            raise ValueError(f"Refusing symlink: {name}")
        shutil.copyfile(source, server_root / name)
    config = {"mcpServers": {"zhenyun-pangu-mcp": {
        "command": "uv",
        "args": ["run", "--frozen", "--directory", str(server_root), "zhenyun-pangu-mcp"],
        "env": {"MCP_ENV_DIR": str(env_dir)},
    }}}
    (output / "mcp.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "AGENTS.md").write_text(
        "# SRM agent 接入\n\n"
        "使用 mcp.json 配置 stdio 服务（需 Python >=3.10 与 uv）；凭据单独放在 MCP_ENV_DIR，"
        "参考 server/.env.example，不在对话或包内存放凭据。移动包后更新配置中的目录绝对路径。\n\n"
        "支持 skills 的客户端加载 skills/*/SKILL.md；不支持自动发现时先按用户目标读取"
        " skills/zhenyun-ops/SKILL.md 并选对应技能。支持 MCP 但无法读本地文件时，"
        "调用 get_workflow_guide；参数以 tools/list 为准。不支持 AGENTS.md 的客户端可把本段"
        "放入其项目说明，不需要虚构 use_skill 或子代理工具。\n\n"
        "业务数据库/ES 只读，发布/绑定不在能力中；证据按环境与租户复用，"
        "修复前核验当前值。知识写入/评论须有对应授权。\n",
        encoding="utf-8",
    )
    return config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New directory; never overwrites an existing bundle")
    parser.add_argument("--env-dir", type=Path, required=True, help="External directory containing .env (not read or copied)")
    args = parser.parse_args()
    export_bundle(args.output, args.env_dir)
    print(f"Exported skills + MCP to {args.output.resolve()}; no credentials copied")


if __name__ == "__main__":
    main()
