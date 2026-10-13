"""Packaging restores local helpers without reading credentials or installing plugins."""

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


HELPER = Path(__file__).resolve().parents[1] / "scripts/plugin_package.py"
SPEC = importlib.util.spec_from_file_location("plugin_package", HELPER)
assert SPEC and SPEC.loader
package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def registry_entry(root, name, plugin_root):
    write_json(root / ".agents/plugins/marketplace.json", {
        "name": name,
        "plugins": [{"name": package.PLUGIN_NAME, "source": {
            "source": "local", "path": str(plugin_root),
        }}],
    })
    return {"name": name, "root": str(root)}


def packaged_plugin(root):
    source_root = HELPER.parent
    manifest = json.loads((source_root / "plugin.manifest.json").read_text())
    mcp = json.loads((source_root / "plugin.mcp.json").read_text())
    write_json(root / ".codex-plugin/plugin.json", manifest)
    write_json(root / ".mcp.json", mcp)
    skill = root / "skills/example"
    write_json(skill / "skill.json", {"name": "example", "entry_point": "SKILL.md"})
    (skill / "SKILL.md").write_text("---\nname: example\ndescription: Example\n---\n")
    for name in package.SERVER_ENV_DIRS:
        server = root / "servers" / name
        server.mkdir(parents=True)
        (server / "pyproject.toml").write_text("[project]\n")
        code = server / "src" / name.replace("-", "_")
        code.mkdir(parents=True)
        (code / "server.py").write_text("# Test runtime\n")
        (server / ".env.example").write_text("TOKEN=your-token-here\n")
    return manifest, mcp


def test_marketplace_uses_registered_root_for_relative_local_source(tmp_path):
    plugin_root = tmp_path / "plugins" / package.PLUGIN_NAME
    entry = registry_entry(tmp_path, "personal", "./plugins/zhenyun-pangu-toolkit")
    assert package.marketplace_name(plugin_root, {"marketplaces": [entry]}) == "personal"


@pytest.mark.parametrize("count", [0, 2])
def test_marketplace_missing_or_ambiguous_matches_fail(tmp_path, count):
    target = tmp_path / "plugin"
    entries = [registry_entry(tmp_path / str(i), f"market-{i}", target) for i in range(count)]
    with pytest.raises(package.PackageError, match=f"found {count}"):
        package.marketplace_name(target, {"marketplaces": entries})


def test_marketplace_ignores_same_plugin_at_a_different_source(tmp_path):
    target = tmp_path / "plugin"
    entries = [registry_entry(tmp_path / "one", "personal", target),
               registry_entry(tmp_path / "two", "other", tmp_path / "other-plugin")]
    assert package.marketplace_name(target, {"marketplaces": entries}) == "personal"


def test_cli_registry_is_read_without_config_access(monkeypatch, tmp_path):
    entry = registry_entry(tmp_path, "personal", tmp_path / "plugin")

    def run(command, **kwargs):
        assert command == ["codex", "plugin", "marketplace", "list", "--json"]
        assert kwargs["timeout"] == 30
        return SimpleNamespace(returncode=0, stdout=json.dumps({"marketplaces": [entry]}))

    monkeypatch.setattr(package.subprocess, "run", run)
    assert package.read_marketplace_name(tmp_path / "plugin") == "personal"


def test_validate_current_package_allows_env_paths_and_examples(tmp_path):
    packaged_plugin(tmp_path)
    package.validate_package(tmp_path)


def test_validate_missing_runtime_fails(tmp_path):
    packaged_plugin(tmp_path)
    (tmp_path / "servers/zhenyun-pangu-mcp/src/zhenyun_pangu_mcp/server.py").unlink()
    with pytest.raises(package.PackageError, match="Missing packaged path"):
        package.validate_package(tmp_path)


def test_validate_escaping_skill_path_fails(tmp_path):
    manifest, _ = packaged_plugin(tmp_path)
    manifest["skills"] = "../outside"
    write_json(tmp_path / ".codex-plugin/plugin.json", manifest)
    with pytest.raises(package.PackageError, match="escapes"):
        package.validate_package(tmp_path)


def test_validate_rejects_embedded_secret_without_reporting_value(tmp_path):
    _, mcp = packaged_plugin(tmp_path)
    mcp["mcpServers"]["zhenyun-pangu-mcp"]["env"]["ACCESS_TOKEN"] = "test-secret-sentinel"
    write_json(tmp_path / ".mcp.json", mcp)
    with pytest.raises(package.PackageError) as caught:
        package.validate_package(tmp_path)
    assert "test-secret-sentinel" not in str(caught.value)


@pytest.mark.parametrize("private_name", [".env", ".auth", "token-cache.json"])
def test_validate_rejects_runtime_credentials_by_name(tmp_path, private_name):
    packaged_plugin(tmp_path)
    path = tmp_path / "servers/zhenyun-pangu-mcp" / private_name
    path.write_text("test-private-sentinel")
    with pytest.raises(package.PackageError) as caught:
        package.validate_package(tmp_path)
    assert "test-private-sentinel" not in str(caught.value)


def test_cachebuster_changes_only_version_and_is_unique_in_same_second(tmp_path):
    manifest, _ = packaged_plugin(tmp_path)
    now = datetime(2026, 10, 13, 4, 5, 6, tzinfo=timezone.utc)
    assert package.update_cachebuster(tmp_path, now=now) == "0.1.0+codex.20261013040506"
    assert package.update_cachebuster(tmp_path, now=now) == "0.1.0+codex.20261013040507"
    updated = json.loads((tmp_path / ".codex-plugin/plugin.json").read_text())
    assert updated.pop("version") == "0.1.0+codex.20261013040507"
    manifest.pop("version")
    assert updated == manifest


def test_cachebuster_rejects_duplicate_version_keys(tmp_path):
    path = tmp_path / ".codex-plugin/plugin.json"
    path.parent.mkdir()
    path.write_text('{"version":"0.1.0","version":"0.2.0"}')
    with pytest.raises(package.PackageError, match="Duplicate JSON key"):
        package.update_cachebuster(tmp_path)
