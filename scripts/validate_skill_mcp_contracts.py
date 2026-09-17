#!/usr/bin/env python3
"""Validate custom skill contracts against both Zhenyun MCP tool surfaces."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve()
MCP_ROOT = SCRIPT_PATH.parents[1]
WORKSPACE_ROOT = SCRIPT_PATH.parents[2]
SCRIPT_PLATFORM_ROOT = WORKSPACE_ROOT / "zhenyun-script-platform-mcp"
SKILLS_ROOT = WORKSPACE_ROOT / "custom-skills"
FRONTMATTER_NAME = re.compile(r"^name:\s*([^\s]+)\s*$", re.MULTILINE)


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)


def main() -> int:
    # Registration must be deterministic even if a developer has locally enabled
    # experimental GitLab search in .env. Skills intentionally target the default
    # supported surface where those two tools are not exposed.
    os.environ["GITLAB_SEARCH_ENABLED"] = "false"
    os.environ["PANGU_EXPOSE_LEGACY_SCRIPT_READ_TOOLS"] = "false"
    sys.path.insert(0, str(MCP_ROOT / "src"))
    sys.path.insert(0, str(SCRIPT_PLATFORM_ROOT / "src"))
    from zhenyun_pangu_mcp import server as pangu_server  # noqa: PLC0415
    from zhenyun_script_platform_mcp import server as script_server  # noqa: PLC0415

    runtime_servers = {
        "zhenyun-pangu-mcp": pangu_server,
        "zhenyun-script-platform-mcp": script_server,
    }

    errors = 0
    # The MCP package is the source of truth; the skill copy ships to clients
    # that can read files but cannot invoke MCP resources/prompts.
    guide_source = MCP_ROOT / "src/zhenyun_pangu_mcp/workflow_guide.md"
    guide_copy = SKILLS_ROOT / "zhenyun-ops/references/collaboration-contract.md"
    if not guide_copy.exists() or guide_copy.read_bytes() != guide_source.read_bytes():
        fail("workflow guide drift: copy packaged workflow_guide.md to zhenyun-ops/references/collaboration-contract.md")
        errors += 1
    declared_tools: dict[str, set[str]] = {name: set() for name in runtime_servers}
    skill_names: set[str] = set()
    skill_tool_counts: dict[str, int] = {}

    for skill_dir in sorted(
        path
        for path in SKILLS_ROOT.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    ):
        skill_md = skill_dir / "SKILL.md"
        manifest_path = skill_dir / "skill.json"
        if not skill_md.exists() or not manifest_path.exists():
            fail(f"{skill_dir.name}: SKILL.md and skill.json are both required")
            errors += 1
            continue

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            fail(f"{manifest_path}: invalid JSON: {exc}")
            errors += 1
            continue

        match = FRONTMATTER_NAME.search(skill_md.read_text(encoding="utf-8"))
        frontmatter_name = match.group(1) if match else ""
        manifest_name = manifest.get("name", "")
        expected_name = skill_dir.name
        if frontmatter_name != expected_name or manifest_name != expected_name:
            fail(
                f"{expected_name}: directory/frontmatter/manifest names differ "
                f"({expected_name!r}, {frontmatter_name!r}, {manifest_name!r})"
            )
            errors += 1
        if expected_name in skill_names:
            fail(f"duplicate skill name: {expected_name}")
            errors += 1
        skill_names.add(expected_name)

        # Catch broken local references before distributing to another agent.
        for target in re.findall(r"\]\(([^)]+)\)", skill_md.read_text(encoding="utf-8")):
            if "://" in target or target.startswith("#"):
                continue
            relative = target.split("#", 1)[0]
            if relative and not (skill_dir / relative).exists():
                fail(f"{expected_name}: missing local reference: {relative}")
                errors += 1

        servers = manifest.get("mcp_servers")
        if not isinstance(servers, list):
            fail(f"{expected_name}: mcp_servers must be a list")
            errors += 1
            continue
        for item in servers:
            if not isinstance(item, dict):
                continue
            server_name = item.get("name")
            if server_name not in runtime_servers:
                fail(f"{expected_name}: unsupported MCP server in manifest: {server_name!r}")
                errors += 1
                continue
            tools = item.get("tools")
            if not isinstance(tools, list) or not all(isinstance(tool, str) for tool in tools):
                fail(f"{expected_name}: {server_name} tools must be a string list")
                errors += 1
                continue
            declared_tools[server_name].update(tools)
            skill_tool_counts[expected_name] = (
                skill_tool_counts.get(expected_name, 0) + len(tools)
            )

    router_path = SKILLS_ROOT / "zhenyun-ops" / "SKILL.md"
    router_text = router_path.read_text(encoding="utf-8") if router_path.exists() else ""
    orphan_skills = sorted(
        skill_name
        for skill_name in skill_names
        if skill_name != "zhenyun-ops" and f"`{skill_name}`" not in router_text
    )
    tool_less_skills = sorted(
        skill_name
        for skill_name in skill_names
        if skill_name != "zhenyun-ops" and skill_tool_counts.get(skill_name, 0) == 0
    )
    if orphan_skills:
        fail(
            "skills are not referenced by the zhenyun-ops router: "
            + ", ".join(orphan_skills)
        )
        errors += 1
    if tool_less_skills:
        fail(
            "non-router skills declare no zhenyun-pangu-mcp tools: "
            + ", ".join(tool_less_skills)
        )
        errors += 1

    runtime_tool_count = 0
    for server_name, server in runtime_servers.items():
        runtime_tools = set(server.mcp._tool_manager._tools)
        runtime_tool_count += len(runtime_tools)
        for name, tool in server.mcp._tool_manager._tools.items():
            if tool.annotations is None or tool.annotations.readOnlyHint is None:
                fail(f"{server_name}.{name}: missing explicit MCP side-effect annotations")
                errors += 1
        missing_tools = sorted(declared_tools[server_name] - runtime_tools)
        orphan_tools = sorted(runtime_tools - declared_tools[server_name])
        if missing_tools:
            fail(
                f"{server_name}: skill manifests reference unavailable tools: "
                + ", ".join(missing_tools)
            )
            errors += 1
        if orphan_tools:
            fail(
                f"{server_name}: MCP tools have no skill owner: " + ", ".join(orphan_tools)
            )
            errors += 1

    if errors:
        return 1
    print(
        f"OK: {len(skill_names)} skills, {runtime_tool_count} MCP tools across "
        f"{len(runtime_servers)} servers, "
        "no missing or orphan skill/tool contracts"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
