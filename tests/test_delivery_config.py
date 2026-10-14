"""Delivery path discovery, refresh, diagnostics and the real MCP contract."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from zhenyun_pangu_mcp import config, delivery_config, server  # noqa: E402


@pytest.fixture
def env_file(monkeypatch, tmp_path):
    file = tmp_path / ".env"
    file.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "_ENV_FILE", file)
    monkeypatch.setattr(config, "_MARMOT_DELIVERY_ROOT_OVERRIDE", None)
    return file


def test_current_dotenv_wins_over_stale_startup_injection(env_file, monkeypatch, tmp_path):
    monkeypatch.setenv("MARMOT_DELIVERY_ROOT", str(tmp_path / "stale"))
    env_file.write_text(f'MARMOT_DELIVERY_ROOT="{tmp_path / "first"}"\n', encoding="utf-8")
    first = delivery_config.get_delivery_config()
    assert first["valid"] is True
    assert first["output_root"] == str(tmp_path / "first")
    assert first["env_file"] == str(env_file)
    assert first["config_source"] == "dotenv"
    assert not (tmp_path / "first").exists()

    env_file.write_text(f"MARMOT_DELIVERY_ROOT={tmp_path / 'second'}\n", encoding="utf-8")
    assert delivery_config.get_delivery_config()["output_root"] == str(tmp_path / "second")
    env_file.write_text("", encoding="utf-8")
    assert delivery_config.get_delivery_config()["status"] == "not_configured"


def test_host_environment_override_has_priority(env_file, monkeypatch, tmp_path):
    env_file.write_text(f"MARMOT_DELIVERY_ROOT={tmp_path / 'file'}\n", encoding="utf-8")
    monkeypatch.setattr(config, "_MARMOT_DELIVERY_ROOT_OVERRIDE", str(tmp_path / "host"))
    result = delivery_config.get_delivery_config()
    assert result["output_root"] == str(tmp_path / "host")
    assert result["config_source"] == "environment"
    monkeypatch.setattr(config, "_MARMOT_DELIVERY_ROOT_OVERRIDE", "")
    assert delivery_config.get_delivery_config()["status"] == "not_configured"


def test_missing_env_file_is_not_reported_as_empty_key(env_file):
    env_file.unlink()
    result = delivery_config.get_delivery_config()
    assert result["status"] == "missing_env_file"
    assert result["env_file"] == str(env_file)
    assert result["configured"] is False
    assert result["output_root"] is None


def test_environment_only_config_does_not_require_dotenv(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_ENV_FILE", None)
    monkeypatch.setattr(config, "_MARMOT_DELIVERY_ROOT_OVERRIDE", str(tmp_path))
    result = delivery_config.get_delivery_config()
    assert result["status"] == "ready"
    assert result["env_file_exists"] is False


@pytest.mark.parametrize("value", ["", "relative/deliveries", "."])
def test_no_implicit_cwd_fallback(env_file, value):
    env_file.write_text(f"MARMOT_DELIVERY_ROOT={value}\n", encoding="utf-8")
    result = delivery_config.get_delivery_config()
    assert result["valid"] is False
    assert result["output_root"] is None
    assert result["status"] == ("not_configured" if not value else "invalid_path")


def test_existing_file_is_not_a_delivery_directory(env_file, tmp_path):
    env_file.write_text(f"MARMOT_DELIVERY_ROOT={env_file}\n", encoding="utf-8")
    result = delivery_config.get_delivery_config()
    assert result["configured"] is True
    assert result["status"] == "not_directory"
    assert result["valid"] is False


def test_writable_ancestor_is_probed_without_creating_directories(env_file, tmp_path):
    root = tmp_path / "new" / "nested"
    env_file.write_text(f"MARMOT_DELIVERY_ROOT={root}\n", encoding="utf-8")
    result = delivery_config.get_delivery_config()
    assert result["status"] == "ready"
    assert result["writable_probe"] == str(tmp_path)
    assert result["exists"] is False
    assert not (tmp_path / "new").exists()


def test_permission_failure_preserves_loaded_path(env_file, monkeypatch, tmp_path):
    env_file.write_text(f"MARMOT_DELIVERY_ROOT={tmp_path}\n", encoding="utf-8")
    monkeypatch.setattr(delivery_config.os, "access", lambda *_args: False)
    result = delivery_config.get_delivery_config()
    assert result["configured"] is True
    assert result["status"] == "not_writable"
    assert result["output_root"] == str(tmp_path)
    assert result["writability_scope"] == "mcp_process"


def test_config_read_error_does_not_leak_exception_contents(env_file, monkeypatch):
    def fail_read(*_args, **_kwargs):
        raise PermissionError("SECRET-DO-NOT-EXPOSE")

    monkeypatch.setattr(delivery_config, "dotenv_values", fail_read)
    result = delivery_config.get_delivery_config()
    assert result["status"] == "config_read_error"
    assert "SECRET-DO-NOT-EXPOSE" not in json.dumps(result)


def test_tool_response_contains_path_but_no_other_config(env_file, tmp_path):
    env_file.write_text(
        f"MARMOT_DELIVERY_ROOT={tmp_path}\nPASSWORD=SECRET-DO-NOT-EXPOSE\n",
        encoding="utf-8",
    )
    response = server.marmot_get_delivery_config()
    result = json.loads(response)
    assert result["ok"] is True
    assert result["valid"] is True
    assert result["meta"]["source"] == "local-config"
    assert "PASSWORD" not in response
    assert "SECRET-DO-NOT-EXPOSE" not in response


def test_stdio_discovery_and_refresh_use_env_dir_instead_of_cwd(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    settings_dir = tmp_path / "settings"
    settings_dir.mkdir()
    env_file = settings_dir / ".env"
    env_file.write_text(f"MARMOT_DELIVERY_ROOT={tmp_path / 'first'}\n", encoding="utf-8")
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / ".env").write_text("MARMOT_DELIVERY_ROOT=/wrong-cwd\n", encoding="utf-8")

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "zhenyun_pangu_mcp"],
            cwd=str(unrelated),
            env={"PYTHONPATH": str(ROOT / "src"), "MCP_ENV_DIR": str(settings_dir)},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tool = next(t for t in (await session.list_tools()).tools
                            if t.name == "marmot_get_delivery_config")
                assert tool.annotations.readOnlyHint is True
                assert tool.annotations.openWorldHint is False
                first = await session.call_tool(tool.name, {})
                assert not first.isError
                assert json.loads(first.content[0].text)["output_root"] == str(tmp_path / "first")
                env_file.write_text(f"MARMOT_DELIVERY_ROOT={tmp_path / 'second'}\n", encoding="utf-8")
                second = await session.call_tool(tool.name, {})
                assert json.loads(second.content[0].text)["output_root"] == str(tmp_path / "second")
                assert not (tmp_path / "first").exists()
                assert not (tmp_path / "second").exists()

    asyncio.run(run())
