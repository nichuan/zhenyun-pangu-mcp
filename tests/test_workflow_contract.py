"""Portable guidance and side effects must survive the actual MCP interface."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from zhenyun_pangu_mcp import server, workflow  # noqa: E402
from zhenyun_pangu_mcp.tool_policy import tool_annotations  # noqa: E402


def test_client_visible_annotations_and_discovery_match():
    advertised = asyncio.run(server.mcp.list_tools())
    capabilities = json.loads(server.get_workflow_guide("capabilities"))
    assert capabilities["backend_health_checked"] is False
    assert {item["name"] for item in capabilities["tools"]} == {tool.name for tool in advertised}
    by_name = {tool.name: tool.annotations for tool in advertised}
    assert all(item is not None for item in by_name.values())
    assert by_name["archery_query"].readOnlyHint is True
    assert by_name["get_workflow_guide"].openWorldHint is False
    for name in ("save_knowledge", "choerodon_add_comment", "record_template_usage", "record_table_usage"):
        assert by_name[name].readOnlyHint is False
        assert by_name[name].idempotentHint is False
    for name in ("delete_knowledge", "update_knowledge", "upsert_table_knowledge", "delete_sql_template"):
        assert by_name[name].destructiveHint is True


def test_new_tools_cannot_silently_default_to_readonly():
    with pytest.raises(ValueError, match="Missing side-effect policy"):
        tool_annotations("new_unclassified_tool")


@pytest.mark.parametrize("topic", workflow.GUIDE_TOPICS)
def test_guide_topics_return_versioned_json(topic):
    result = json.loads(server.get_workflow_guide(topic))
    assert result["ok"] is True
    assert result["contract_version"] == 1
    assert result["topic"] == topic
    assert result["content"] == workflow.get_guide(topic)
    assert result["meta"]["source"] == "workflow-contract"


def test_unknown_topic_fails_without_business_access():
    result = json.loads(server.get_workflow_guide("invalid"))
    assert result["ok"] is False
    assert result["error"]["retryable"] is False


def test_stdio_client_can_discover_and_read_guide_without_credentials(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "zhenyun_pangu_mcp"],
            env={"PYTHONPATH": str(ROOT / "src"), "MCP_ENV_DIR": str(tmp_path), "GITLAB_SEARCH_ENABLED": "false"},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                assert "get_workflow_guide" in initialized.instructions
                tools = (await session.list_tools()).tools
                assert "gitlab_search_code" not in {tool.name for tool in tools}
                assert all(tool.annotations is not None for tool in tools)
                response = await session.call_tool("get_workflow_guide", {"topic": "handoff"})
                assert not response.isError
                assert json.loads(response.content[0].text)["topic"] == "handoff"
                invalid = await session.call_tool("get_workflow_guide", {"topic": "invalid"})
                assert invalid.isError

    asyncio.run(run())


def _exporter():
    spec = importlib.util.spec_from_file_location("export_agent_bundle", ROOT / "scripts/export_agent_bundle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_export_excludes_credentials_caches_and_git(tmp_path, monkeypatch):
    exporter = _exporter()
    mcp_root, skill_root = tmp_path / "mcp", tmp_path / "skills"
    (mcp_root / "src/pkg").mkdir(parents=True)
    (skill_root / "sample/.git").mkdir(parents=True)
    (skill_root / "sample/SKILL.md").write_text("skill")
    (skill_root / "sample/.git/config").write_text("private")
    (mcp_root / "src/pkg/main.py").write_text("# code")
    (mcp_root / "src/pkg/__pycache__").mkdir()
    (mcp_root / "src/pkg/__pycache__/cached.py").write_text("private")
    (mcp_root / ".env").write_text("TOKEN=must-not-copy")
    for name in ("pyproject.toml", "uv.lock", "README.md", "WORKFLOW_OPTIMIZATION.md", ".env.example"):
        (mcp_root / name).write_text("example")
    monkeypatch.setattr(exporter, "MCP_ROOT", mcp_root)
    monkeypatch.setattr(exporter, "SKILLS_ROOT", skill_root)
    output = tmp_path / "bundle"
    config = exporter.export_bundle(output, tmp_path / "credentials")
    paths = {str(p.relative_to(output)) for p in output.rglob("*") if p.is_file()}
    assert "server/src/pkg/main.py" in paths
    assert "skills/sample/SKILL.md" in paths
    assert not any("__pycache__" in p or "/.git/" in p or p.endswith("/.env") for p in paths)
    assert all("must-not-copy" not in p.read_text() for p in output.rglob("*") if p.is_file())
    assert config["mcpServers"]["zhenyun-pangu-mcp"]["env"] == {"MCP_ENV_DIR": str(tmp_path / "credentials")}
    with pytest.raises(FileExistsError):
        exporter.export_bundle(output, tmp_path / "credentials")
    with pytest.raises(ValueError, match="outside MCP"):
        exporter.export_bundle(mcp_root / "bundle", tmp_path / "credentials")
    with pytest.raises(ValueError, match="Credential directory"):
        exporter.export_bundle(tmp_path / "other", tmp_path / "other/secrets")


def test_export_rejects_symlinked_sources(tmp_path):
    exporter = _exporter()
    source = tmp_path / "source"
    source.mkdir()
    secret = tmp_path / "secret.py"
    secret.write_text("private")
    (source / "link.py").symlink_to(secret)
    with pytest.raises(ValueError, match="symlink"):
        exporter._copy_sources(source, tmp_path / "out", {".py"})
