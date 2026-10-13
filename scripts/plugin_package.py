#!/usr/bin/env python3
"""Local packaging helpers for the Zhenyun Codex plugin; no network or credentials."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PLUGIN_NAME = "zhenyun-pangu-toolkit"
SERVER_ENV_DIRS = {
    "zhenyun-pangu-mcp": "MCP_ENV_DIR",
    "zhenyun-script-platform-mcp": "SCRIPT_PLATFORM_ENV_DIR",
}
VERSION_RE = re.compile(r"^(\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?)(?:\+codex\.(\d{14}))?$")
PRIVATE_NAMES = {".env", ".auth", ".tmp", "token.json", "tokens.json", "token-cache.json"}


class PackageError(ValueError):
    """A package or local registry does not meet the supported contract."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PackageError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _json_object(text: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(text, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, PackageError) as exc:
        raise PackageError(f"Invalid JSON in {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise PackageError(f"{label} must contain a JSON object")
    return value


def _read_object(path: Path) -> dict[str, Any]:
    try:
        return _json_object(path.read_text(encoding="utf-8"), str(path))
    except OSError as exc:
        raise PackageError(f"Cannot read {path}: {exc.strerror}") from exc


def marketplace_name(plugin_root: Path, registry: dict[str, Any]) -> str:
    """Resolve one CLI-registered local source by its actual canonical path."""
    marketplaces = registry.get("marketplaces")
    if not isinstance(marketplaces, list):
        raise PackageError("Codex marketplace list must contain a marketplaces array")
    target = plugin_root.expanduser().resolve()
    matches: list[str] = []
    for item in marketplaces:
        if not isinstance(item, dict):
            raise PackageError("Invalid registered marketplace entry")
        name, root = item.get("name"), item.get("root")
        if not isinstance(name, str) or not name or not isinstance(root, str) or not root:
            raise PackageError("Registered marketplace must have a name and root")
        registered_root = Path(root).expanduser().resolve()
        manifest_path = registered_root / ".agents/plugins/marketplace.json"
        if not manifest_path.is_file():
            continue
        manifest = _read_object(manifest_path)
        if manifest.get("name") != name:
            raise PackageError(f"Registered marketplace name differs from manifest at {manifest_path}")
        plugins = manifest.get("plugins")
        if not isinstance(plugins, list):
            raise PackageError(f"Marketplace plugins must be an array at {manifest_path}")
        for plugin in plugins:
            if not isinstance(plugin, dict) or plugin.get("name") != PLUGIN_NAME:
                continue
            source = plugin.get("source")
            if not isinstance(source, dict) or source.get("source") != "local":
                continue
            source_path = source.get("path")
            if not isinstance(source_path, str) or not source_path:
                raise PackageError(f"Local plugin source has no path at {manifest_path}")
            resolved_source = Path(source_path).expanduser()
            if not resolved_source.is_absolute():
                resolved_source = registered_root / resolved_source
            if resolved_source.resolve() == target:
                matches.append(name)
    if len(matches) != 1:
        raise PackageError(
            f"Expected one registered local marketplace for {PLUGIN_NAME} at {target}; "
            f"found {len(matches)}"
        )
    return matches[0]


def read_marketplace_name(plugin_root: Path) -> str:
    try:
        result = subprocess.run(
            ["codex", "plugin", "marketplace", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PackageError("Cannot read registered Codex marketplaces") from exc
    if result.returncode:
        raise PackageError(f"Codex marketplace list failed with exit code {result.returncode}")
    return marketplace_name(plugin_root, _json_object(result.stdout, "Codex marketplace list"))


def _packaged_path(root: Path, value: Any, label: str, *, directory: bool = False) -> Path:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise PackageError(f"{label} must be a relative package path")
    path = (root / value).resolve()
    if not path.is_relative_to(root):
        raise PackageError(f"{label} escapes the plugin package")
    if not (path.is_dir() if directory else path.is_file()):
        raise PackageError(f"Missing packaged path for {label}: {value}")
    return path


def validate_package(plugin_root: Path) -> None:
    root = plugin_root.expanduser().resolve()
    manifest = _read_object(root / ".codex-plugin/plugin.json")
    if manifest.get("name") != PLUGIN_NAME:
        raise PackageError(f"Plugin manifest name must be {PLUGIN_NAME}")
    version = manifest.get("version")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise PackageError("Plugin manifest has an unsupported version")
    if not isinstance(manifest.get("description"), str) or not manifest["description"].strip():
        raise PackageError("Plugin manifest requires a description")
    skills_root = _packaged_path(root, manifest.get("skills"), "skills", directory=True)
    skill_dirs = sorted(path for path in skills_root.iterdir() if path.is_dir() and not path.name.startswith("."))
    if not skill_dirs:
        raise PackageError("Plugin package has no skills")
    for skill_dir in skill_dirs:
        _packaged_path(root, str((skill_dir / "SKILL.md").relative_to(root)), f"{skill_dir.name}/SKILL.md")
        skill_manifest = _read_object(skill_dir / "skill.json")
        if skill_manifest.get("name") != skill_dir.name or skill_manifest.get("entry_point") != "SKILL.md":
            raise PackageError(f"Invalid packaged skill manifest: {skill_dir.name}")

    mcp_path = _packaged_path(root, manifest.get("mcpServers"), "mcpServers")
    servers = _read_object(mcp_path).get("mcpServers")
    if not isinstance(servers, dict) or set(servers) != set(SERVER_ENV_DIRS):
        raise PackageError("Plugin package must declare exactly the two supported Zhenyun MCP servers")
    for name, env_dir in SERVER_ENV_DIRS.items():
        server = servers[name]
        if not isinstance(server, dict):
            raise PackageError(f"Invalid MCP configuration for {name}")
        if server.get("command") != "uv" or server.get("args") != ["run", "--project", ".", name]:
            raise PackageError(f"Unsupported MCP command or arguments for {name}")
        cwd = _packaged_path(root, server.get("cwd"), f"{name}.cwd", directory=True)
        if cwd != root / "servers" / name:
            raise PackageError(f"MCP cwd must point to servers/{name}")
        _packaged_path(root, f"servers/{name}/pyproject.toml", f"{name}/pyproject.toml")
        _packaged_path(root, f"servers/{name}/src/{name.replace('-', '_')}/server.py", f"{name}/server.py")
        env = server.get("env")
        if not isinstance(env, dict) or set(env) != {env_dir}:
            raise PackageError(f"MCP env for {name} may contain only its environment directory path")
        value = env[env_dir]
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise PackageError(f"{env_dir} must be an absolute environment directory path")

    # Credential caches live beside each runtime, not in its Python source tree.
    # Check their locations without reading dotenv or token contents.
    for directory in (root, *(root / "servers" / name for name in SERVER_ENV_DIRS)):
        for path in directory.iterdir():
            if path.name.lower() in PRIVATE_NAMES:
                raise PackageError(f"Private runtime file or directory is packaged: {path.relative_to(root)}")


def update_cachebuster(plugin_root: Path, *, now: datetime | None = None) -> str:
    """Replace only the manifest version; repeated calls advance the time suffix."""
    path = plugin_root.expanduser().resolve() / ".codex-plugin/plugin.json"
    manifest = _read_object(path)
    current = manifest.get("version")
    match = VERSION_RE.fullmatch(current) if isinstance(current, str) else None
    if match is None:
        raise PackageError("Plugin manifest has an unsupported version")
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    if match.group(2):
        try:
            previous = datetime.strptime(match.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise PackageError("Plugin version has an invalid cachebuster timestamp") from exc
        stamp = max(stamp, previous + timedelta(seconds=1))
    version = f"{match.group(1)}+codex.{stamp.strftime('%Y%m%d%H%M%S')}"
    manifest["version"] = version
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return version


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("marketplace-name", "validate", "cachebuster"))
    parser.add_argument("plugin_root", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "marketplace-name":
            print(read_marketplace_name(args.plugin_root))
        elif args.action == "validate":
            validate_package(args.plugin_root)
            print("Plugin package is valid")
        else:
            print(update_cachebuster(args.plugin_root))
    except PackageError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
