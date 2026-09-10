#!/usr/bin/env python3
"""Validate the contract between custom skills and zhenyun-pangu-mcp tools."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve()
MCP_ROOT = SCRIPT_PATH.parents[1]
WORKSPACE_ROOT = SCRIPT_PATH.parents[2]
SKILLS_ROOT = WORKSPACE_ROOT / "custom-skills"
FRONTMATTER_NAME = re.compile(r"^name:\s*([^\s]+)\s*$", re.MULTILINE)


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)


def main() -> int:
    # Registration must be deterministic even if a developer has locally enabled
    # experimental GitLab search in .env. Skills intentionally target the default
    # supported surface where those two tools are not exposed.
    os.environ["GITLAB_SEARCH_ENABLED"] = "false"
    sys.path.insert(0, str(MCP_ROOT / "src"))
    from zhenyun_pangu_mcp import server  # noqa: PLC0415

    errors = 0
    declared_tools: set[str] = set()
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

        servers = manifest.get("mcp_servers")
        if not isinstance(servers, list):
            fail(f"{expected_name}: mcp_servers must be a list")
            errors += 1
            continue
        for item in servers:
            if not isinstance(item, dict) or item.get("name") != "zhenyun-pangu-mcp":
                continue
            tools = item.get("tools")
            if not isinstance(tools, list) or not all(isinstance(tool, str) for tool in tools):
                fail(f"{expected_name}: zhenyun-pangu-mcp tools must be a string list")
                errors += 1
                continue
            declared_tools.update(tools)
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

    runtime_tools = set(server.mcp._tool_manager._tools)
    missing_tools = sorted(declared_tools - runtime_tools)
    orphan_tools = sorted(runtime_tools - declared_tools)
    if missing_tools:
        fail("skill manifests reference unavailable MCP tools: " + ", ".join(missing_tools))
        errors += 1
    if orphan_tools:
        fail("MCP tools have no skill owner: " + ", ".join(orphan_tools))
        errors += 1

    if errors:
        return 1
    print(
        f"OK: {len(skill_names)} skills, {len(runtime_tools)} MCP tools, "
        "no missing or orphan skill/tool contracts"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
