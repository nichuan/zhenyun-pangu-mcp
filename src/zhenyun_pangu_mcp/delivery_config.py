"""Read only the local Marmot delivery path; never create delivery artifacts."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values

from . import config


def get_delivery_config() -> dict[str, object]:
    """Refresh the selected .env on every call, preserving host env precedence."""
    env_file = config._ENV_FILE
    override = config._MARMOT_DELIVERY_ROOT_OVERRIDE
    data: dict[str, object] = {
        "source": "MARMOT_DELIVERY_ROOT",
        "config_source": "environment" if override is not None else "dotenv",
        "env_file": str(env_file) if env_file is not None else None,
        "env_file_exists": False,
        "configured": False,
        "valid": False,
        "output_root": None,
        "exists": False,
        "is_directory": False,
        "writable": False,
        "writable_probe": None,
        "writability_scope": "mcp_process",
    }

    def finish(status: str, message: str) -> dict[str, object]:
        return {**data, "status": status, "message": message}

    try:
        data["env_file_exists"] = env_file is not None and env_file.is_file()
        if override is not None:
            raw = override.strip()
        elif not data["env_file_exists"]:
            return finish(
                "missing_env_file",
                "未找到 MCP 实际加载的 .env，请核对 MCP_ENV_DIR；不能据此认定用户未维护交付路径。",
            )
        else:
            # Read one key from the current file without mutating os.environ or
            # exposing credentials. dotenv interpolation matches startup loading.
            raw = (dotenv_values(env_file).get("MARMOT_DELIVERY_ROOT") or "").strip()
    except (OSError, UnicodeError):
        return finish("config_read_error", "无法读取 MCP 的 .env，请核对配置文件权限。")

    if not raw:
        return finish(
            "not_configured",
            "当前有效配置中 MARMOT_DELIVERY_ROOT 为空，请配置交付根目录或提供本次输出目录。",
        )
    data["configured"] = True
    try:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute() or "\x00" in raw:
            return finish("invalid_path", "MARMOT_DELIVERY_ROOT 必须是绝对目录路径，可使用 ~/...。")
        output_root = candidate.resolve()
        data["output_root"] = str(output_root)
        data["exists"] = output_root.exists()
        data["is_directory"] = output_root.is_dir()
        if data["exists"] and not data["is_directory"]:
            return finish("not_directory", "MARMOT_DELIVERY_ROOT 指向已有文件，必须改为目录路径。")

        probe = output_root
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        data["writable_probe"] = str(probe)
        data["writable"] = probe.is_dir() and os.access(probe, os.W_OK | os.X_OK)
        data["valid"] = bool(data["writable"])
        if not data["valid"]:
            return finish("not_writable", "交付目录或最近的已存在父目录不可写，请核对路径和权限。")
        return finish("ready", "交付配置有效，可直接使用 output_root，无需再次询问目录。")
    except (OSError, RuntimeError, ValueError):
        return finish("path_check_error", "无法检查交付路径，请核对路径及文件系统权限。")
