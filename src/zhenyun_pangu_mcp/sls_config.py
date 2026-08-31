"""系统/环境 → 阿里云 SLS(project/logstore/namespace)映射与凭据加载。

对接方式说明(2026-08 现状):
- cn 国内公有云盘古【全部环境】走阿里云 SLS:
  - prod → pangu-cn-saas-3-prod-shared-sls-project-0 / sls-store-0-pangu-prod / saas-prod
  - dev  → pangu-cn-saas-3-nonprod-shared-sls-project-0 / sls-store-0-pangu-nonprod / saas-dev-new
  - test → pangu-cn-saas-3-nonprod-shared-sls-project-0 / sls-store-0-pangu-nonprod / saas-test-new
- 盘古非生产(dev/test)曾短暂迁移到 Loki(logs.going-link.net),现已迁回阿里云 SLS,
  Loki 仅保留 AWS 海外(jp-saas-1),故查国内盘古日志一律走本模块。
- 天工为兼容保留(历史上由 log-ops-mcp 提供),同样走阿里云 SLS。

凭据从环境变量读取(SLS_PANGU_PROD_* / SLS_PANGU_NONPROD_* / SLS_TYGO_*),永不落盘到文档。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from dotenv import load_dotenv

# 与 config.py 保持一致:支持 MCP_ENV_DIR 显式指定 .env 目录
_ENV_DIR = os.getenv("MCP_ENV_DIR")
if _ENV_DIR:
    load_dotenv(os.path.join(_ENV_DIR, ".env"))
else:
    load_dotenv()


@dataclass(frozen=True)
class SlsTarget:
    system: str
    environment: str
    project: str
    logstore: str
    namespace: str
    key_env_prefix: str


_TARGETS = {
    # 盘古(甄云盘古)：prod 与非生产(dev/test)均在阿里云 SLS
    ("盘古", "dev"): ("pangu-cn-saas-3-nonprod-shared-sls-project-0", "sls-store-0-pangu-nonprod", "saas-dev-new", "PANGU_NONPROD"),
    ("盘古", "test"): ("pangu-cn-saas-3-nonprod-shared-sls-project-0", "sls-store-0-pangu-nonprod", "saas-test-new", "PANGU_NONPROD"),
    ("盘古", "prod"): ("pangu-cn-saas-3-prod-shared-sls-project-0", "sls-store-0-pangu-prod", "saas-prod", "PANGU_PROD"),
    # 天工(兼容保留)
    ("天工", "paas-dev"): ("tygo-cn-saas-1-nonprod-shared-sls-project-0", "sls-store-0-tiangong", "tygo-paas-dev", "TYGO_NONPROD"),
    ("天工", "paas-test"): ("tygo-cn-saas-1-nonprod-shared-sls-project-0", "sls-store-0-tiangong", "tygo-paas-test", "TYGO_NONPROD"),
    ("天工", "saas-dev"): ("tygo-cn-saas-1-nonprod-shared-sls-project-0", "sls-store-0-tiangong", "tygo-saas-dev", "TYGO_NONPROD"),
    ("天工", "saas-test"): ("tygo-cn-saas-1-nonprod-shared-sls-project-0", "sls-store-0-tiangong", "tygo-saas-test", "TYGO_NONPROD"),
    ("天工", "sandbox"): ("tygo-cn-saas-1-prod-shared-sls-project-0", "sls-store-0-tiangong-prod", "tygo-sandbox", "TYGO_PROD"),
    ("天工", "prod"): ("tygo-cn-saas-1-prod-shared-sls-project-0", "sls-store-0-tiangong-prod", "tygo-saas-prod", "TYGO_PROD"),
}

_ALIASES = {
    "tiangong": "天工",
    "pangu": "盘古",
    "盘古系统": "盘古",
    "天工系统": "天工",
}

# 环境别名：把常见口语化写法归一化到 _TARGETS 的键
_ENV_ALIASES = {
    "product": "prod",
    "pro": "prod",
    "生产": "prod",
    "正式": "prod",
    "线上": "prod",
    "development": "dev",
    "开发": "dev",
    "testing": "test",
    "测试": "test",
    "uat": "test",
}


def supported_targets() -> list[dict]:
    """列出全部受支持的 (system, environment) → project/logstore/namespace 映射（不含凭据）。"""
    return [
        {
            "system": system,
            "environment": environment,
            "project": project,
            "logstore": logstore,
            "namespace": namespace,
        }
        for (system, environment), (project, logstore, namespace, _prefix) in _TARGETS.items()
    ]


def resolve_target(system: str, environment: str) -> SlsTarget:
    system = _ALIASES.get(system.strip().lower(), system.strip())
    environment = _ENV_ALIASES.get(environment.strip().lower(), environment.strip().lower())
    try:
        project, logstore, namespace, prefix = _TARGETS[(system, environment)]
    except KeyError as exc:
        supported = ", ".join(f"{s}/{e}" for s, e in _TARGETS)
        raise ValueError(f"不支持的系统/环境: {system}/{environment}。可选: {supported}") from exc
    return SlsTarget(system, environment, project, logstore, namespace, prefix)


def _load_json_mapping() -> dict:
    raw = os.getenv("LOGOPS_ACCESS_KEYS_JSON", "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("LOGOPS_ACCESS_KEYS_JSON 不是有效 JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError("LOGOPS_ACCESS_KEYS_JSON 必须是对象")
    return value


def credentials(target: SlsTarget) -> tuple[str, str]:
    """加载目标环境的 SLS AccessKey,支持环境变量或单个 JSON 映射。"""
    prefix = target.key_env_prefix
    ak_id = os.getenv(f"SLS_{prefix}_ACCESS_KEY_ID", "").strip()
    ak_secret = os.getenv(f"SLS_{prefix}_ACCESS_KEY_SECRET", "").strip()
    if not ak_id or not ak_secret:
        mapped = _load_json_mapping().get(prefix) or _load_json_mapping().get(target.project) or {}
        ak_id = ak_id or str(mapped.get("access_key_id", "")).strip()
        ak_secret = ak_secret or str(mapped.get("access_key_secret", "")).strip()
    if not ak_id or not ak_secret:
        raise RuntimeError(
            f"缺少 {prefix} 的 SLS 凭据,请配置 SLS_{prefix}_ACCESS_KEY_ID/SECRET "
            "或 LOGOPS_ACCESS_KEYS_JSON。"
        )
    return ak_id, ak_secret


def endpoint() -> str:
    return os.getenv("SLS_ENDPOINT", "cn-shanghai.log.aliyuncs.com").strip()
