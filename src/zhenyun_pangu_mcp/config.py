"""全局配置:从环境变量 / .env 读取,带默认值。

本模块不依赖任何外部仓库目录;所有路径均指向本 MCP 自身或用户显式配置的位置。

键名与 .env.example 严格一致(支持 MCP_ENV_DIR 指定 .env 所在目录)。
"""

from __future__ import annotations

import json as _json
import os
from pathlib import Path

from dotenv import load_dotenv

# uvx/uv run 下 cwd 不一定是项目目录,支持用 MCP_ENV_DIR 显式指定 .env 目录
_ENV_DIR = os.getenv("MCP_ENV_DIR")
if _ENV_DIR:
    load_dotenv(os.path.join(_ENV_DIR, ".env"))
else:
    load_dotenv()

# 包根目录(用于回退默认路径)
PKG_ROOT = Path(__file__).resolve().parent.parent.parent  # .../zhenyun-pangu-mcp
REPO_ROOT = PKG_ROOT.parent  # 仓库根(同仓 zhenyun-tools)

# ---------------------------------------------------------------------------
# Archery 数据库(双站点:cn 国内 / aws 日本云)
# ---------------------------------------------------------------------------
# 各站点 BASE_URL(默认取真实网关,可被 .env 覆盖)
ARCHERY_BASE_URLS = {
    "cn": os.getenv(
        "ARCHERY_BASE_URL",
        os.getenv("ARCHERY_API_BASE_CN", "https://archery.cn-saas-1.tygo.going-link.net"),
    ),
    "aws": os.getenv(
        "ARCHERY_AWS_BASE_URL",
        os.getenv("ARCHERY_API_BASE_AWS", "https://archery.jp-saas-1.going-link.net"),
    ),
}
# 各站点凭据(cn/aws 独立账号密码)
ARCHERY_CREDENTIALS = {
    "cn": (os.getenv("ARCHERY_USERNAME", ""), os.getenv("ARCHERY_PASSWORD", "")),
    "aws": (
        os.getenv("ARCHERY_AWS_USERNAME", os.getenv("ARCHERY_USERNAME", "")),
        os.getenv("ARCHERY_AWS_PASSWORD", os.getenv("ARCHERY_PASSWORD", "")),
    ),
}
# 实例别名 -> 真实 Archery 实例名（**按 site 分组**）。
# 关键：aws 实例只属于 aws 站点，cn 实例只属于 cn 站点。调用方必须显式传 site，
# 否则会因「用 cn 站点去查 aws 实例」而报「未关联该实例」的歧义错误。
# 默认映射为真实实例名;可用 ARCHERY_DB_* 覆盖,或用 ARCHERY_INSTANCE_ALIASES(JSON) 整体覆盖。
# aws 站点当前只有一个正式环境:JP-SaaS-1-Prod-RW-8.0(库 srm)。
ARCHERY_INSTANCE_ALIASES = {
    "cn": {
        "prod": os.getenv("ARCHERY_DB_CN", "SAAS-SRM-PROD数据库"),
        "prod-ro": os.getenv("ARCHERY_DB_CN_RO", "SAAS-SRM-PROD只读数据库"),
        "dev": os.getenv("ARCHERY_DB_DEV", "SAAS-SRM-DEV数据库"),
        "test": os.getenv("ARCHERY_DB_TEST", "SAAS-SRM-TEST数据库"),
    },
    "aws": {
        "aws": os.getenv("ARCHERY_DB_AWS", "JP-SaaS-1-Prod-RW-8.0"),
        "aws-prod": os.getenv("ARCHERY_DB_AWS_RO", "JP-SaaS-1-Prod-RW-8.0"),
    },
}
# 允许 .env 整体覆盖别名映射(JSON, 结构需为 {"cn": {...}, "aws": {...}})
_alias_env = os.getenv("ARCHERY_INSTANCE_ALIASES")
if _alias_env:
    try:
        _parsed = _json.loads(_alias_env)
        if isinstance(_parsed, dict):
            ARCHERY_INSTANCE_ALIASES = {
                str(site): {str(k): str(v) for k, v in aliases.items()}
                for site, aliases in _parsed.items()
            }
    except (ValueError, TypeError):
        pass
# 默认数据库
ARCHERY_DEFAULT_DB = os.getenv("ARCHERY_DEFAULT_DB", "srm")

# ---------------------------------------------------------------------------
# Loki 日志(仅 AWS 海外 jp-saas-1)
#
# 路由现状(2026-08)：国内公有云盘古日志(prod/dev/test 全部环境)已迁回阿里云 SLS，
# 由 sls_config.py 按环境映射 project/logstore/namespace；Loki 只服务 AWS 海外。
# 盘古非生产曾短暂迁移到 Loki(logs.going-link.net)，现相关配置(LOKI_API_BASE_CN /
# CN_LOG_* / CN_LOG_DS_*)已移除，传 region="cn" 会由工具返回明确的重定向提示。
# ---------------------------------------------------------------------------
LOKI_PLATFORMS = {
    "aws": {
        "label": "AWS 海外(jp-saas-1)",
        "url": os.getenv("LOKI_API_BASE_AWS", "https://logs.jp-saas-1.going-link.net"),
        "username": os.getenv("AWS_LOG_USERNAME", os.getenv("LOG_USERNAME", "")),
        "password": os.getenv("AWS_LOG_PASSWORD", os.getenv("LOG_PASSWORD", "")),
    },
}
# 各场景数据源(LogQL 中的 {ds="..."} 或 {source="..."});为空时回退到 query 直接指定
LOKI_DATASOURCES = {
    "aws": {
        "prod": os.getenv("AWS_LOG_DS_PROD", "prod"),
        "nonprod": os.getenv("AWS_LOG_DS_NONPROD", "nonprod"),
        "ops": os.getenv("AWS_LOG_DS_OPS", "ops"),
    },
}

# ---------------------------------------------------------------------------
# 猪齿鱼 Choerodon(内置 Python 客户端,四步纯 HTTP 登录,无外部脚本/浏览器)
# 基址为真实网关 open-gateway.going-link.com(非 code.choerodon.com.cn)
# ---------------------------------------------------------------------------
CHOERODON_BASE_URL = os.getenv("CHOERODON_BASE_URL", "https://open-gateway.going-link.com")
CHOERODON_ORG_ID = os.getenv("CHOERODON_ORG_ID", "1")
CHOERODON_TENANT_ID = os.getenv("CHOERODON_TENANT_ID", "1")
# 默认项目 ID(搜索/详情默认项目;可用 CHOERODON_PROJECT_ID 覆盖)。
# 生产项目 58 的 detail 接口实测可用;敏捷项目 738424127719677952 的 detail 解密不兼容。
CHOERODON_PROJECT_ID = os.getenv("CHOERODON_PROJECT_ID", "58")
CHOERODON_USERNAME = os.getenv("CHOERODON_USERNAME", "")
CHOERODON_PASSWORD = os.getenv("CHOERODON_PASSWORD", "")
CHOERODON_TOKEN_CACHE = os.getenv("CHOERODON_TOKEN_CACHE") or str(Path.home() / ".choerodon_token.json")


# ---------------------------------------------------------------------------
# GitLab 代码平台(整合自 gitlab-code-mcp;token 优先,缺失回退用户名密码)
# 默认指向云原生 SRM 仓库网关
# ---------------------------------------------------------------------------
GITLAB_BASE_URL = os.getenv("GITLAB_BASE_URL", "https://code.choerodon.com.cn")
GITLAB_TOKEN = os.getenv("GITLAB_TOKEN", "")
GITLAB_USERNAME = os.getenv("GITLAB_USERNAME", "")
GITLAB_PASSWORD = os.getenv("GITLAB_PASSWORD", "")
# 当前自托管 GitLab 未启用可靠的项目/代码搜索。默认不向 MCP 客户端暴露
# gitlab_search_*，避免 Agent 先等待失败再回退本地；精确的分支/目录/文件读取
# 能力仍可独立使用。平台能力恢复后需显式开启。
GITLAB_SEARCH_ENABLED = os.getenv("GITLAB_SEARCH_ENABLED", "false").strip().lower() in {
    "1", "true", "yes", "on",
}
# 代码搜索根目录(限定在该 group / 根 project 下,避免全站噪声)
# 仅传其一:PROJECT_ID 优先;GROUP 用于 /search 范围限定
GITLAB_SEARCH_ROOT_ID = os.getenv("GITLAB_SEARCH_ROOT_ID", "")
GITLAB_SEARCH_ROOT_GROUP = os.getenv("GITLAB_SEARCH_ROOT_GROUP", "srm")
GITLAB_SEARCH_DEFAULT_SCOPE = os.getenv("GITLAB_SEARCH_DEFAULT_SCOPE", "srm")

# ---------------------------------------------------------------------------
# 跨仓代码搜索根目录(纯本地遍历)。
# 默认回退到本 MCP 包目录(PKG_ROOT),而非整仓上层(REPO_ROOT),避免 search_repo
# 在未显式配置时递归遍历整个项目树(含 .venv、__pycache__ 等),导致扫描失控/超时。
# 需要跨仓检索时,请在 .env 显式配置 PG_ROOT 指向目标仓库目录。
# ---------------------------------------------------------------------------
PG_ROOT = os.getenv("PG_ROOT", str(PKG_ROOT))

# ---------------------------------------------------------------------------
# 适配器脚本读取（数据库保存 Base64(UTF-16BE)，MCP 边界内统一解码）
# ---------------------------------------------------------------------------
try:
    ADAPTER_SCRIPT_CACHE_MAX_ENTRIES = max(
        1, int(os.getenv("ADAPTER_SCRIPT_CACHE_MAX_ENTRIES", "256"))
    )
except ValueError:
    ADAPTER_SCRIPT_CACHE_MAX_ENTRIES = 256
try:
    ADAPTER_SCRIPT_CACHE_TTL_SECONDS = max(
        1, int(os.getenv("ADAPTER_SCRIPT_CACHE_TTL_SECONDS", "60"))
    )
except ValueError:
    ADAPTER_SCRIPT_CACHE_TTL_SECONDS = 60
try:
    ADAPTER_SCRIPT_DEFAULT_LINES = max(
        1, int(os.getenv("ADAPTER_SCRIPT_DEFAULT_LINES", "200"))
    )
except ValueError:
    ADAPTER_SCRIPT_DEFAULT_LINES = 200
try:
    ADAPTER_SCRIPT_MAX_RANGE_LINES = max(
        1, int(os.getenv("ADAPTER_SCRIPT_MAX_RANGE_LINES", "500"))
    )
except ValueError:
    ADAPTER_SCRIPT_MAX_RANGE_LINES = 500
try:
    ADAPTER_SCRIPT_MAX_RANGE_CHARS = max(
        1000, int(os.getenv("ADAPTER_SCRIPT_MAX_RANGE_CHARS", "200000"))
    )
except ValueError:
    ADAPTER_SCRIPT_MAX_RANGE_CHARS = 200000

# ---------------------------------------------------------------------------
# 知识库 Supabase(知识/模板/表/关系 四张表所在元数据库,不存业务数据)
# ---------------------------------------------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")


def get_supabase_url() -> str:
    if not SUPABASE_URL:
        raise RuntimeError("缺少 SUPABASE_URL，请在 .env 配置知识库连接。")
    return SUPABASE_URL


def get_supabase_key() -> str:
    if not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("缺少 SUPABASE_SERVICE_ROLE_KEY，请在 .env 配置知识库连接。")
    return SUPABASE_SERVICE_ROLE_KEY


# 知识表名(默认不变,可覆盖以指向其他元数据库)
KNOWLEDGE_TABLE = os.getenv("KNOWLEDGE_TABLE", "knowledge_docs")
SQL_TEMPLATE_TABLE = os.getenv("SQL_TEMPLATE_TABLE", "sql_templates")
TABLE_CATALOG_TABLE = os.getenv("TABLE_CATALOG_TABLE", "table_catalog")
TABLE_RELATION_TABLE = os.getenv("TABLE_RELATION_TABLE", "table_relations")


# ---------------------------------------------------------------------------
# 语义向量（Cloudflare Workers AI @cf/qwen/qwen3-embedding-0.6b）
#
# 免费层每天 10,000 Neurons，对本知识库规模足够；模型输出 1024 维，
# embedding 列为 vector(1024)。未配置 key 时检索自动降级为关键词。
#
# 历史说明：早期支持 NVIDIA / Voyage 双 provider 并预留 embedding_voyage 双列做 A/B，
# 向量列一度为 vector(2048)。因 nv-embed-v1 下线、Voyage 免费额度限速（3 RPM），
# 现整体切到 Cloudflare Workers AI，向量统一写单列 embedding（vector(1024)）。
# ---------------------------------------------------------------------------
def get_cf_api_token() -> str:
    return os.getenv("CF_API_TOKEN", "").strip()


def get_cf_account_id() -> str:
    return os.getenv("CF_ACCOUNT_ID", "").strip()


def get_cf_embed_model() -> str:
    return os.getenv("CF_EMBED_MODEL", "@cf/qwen/qwen3-embedding-0.6b").strip()


def get_cf_embed_dimension() -> int:
    try:
        return int(os.getenv("CF_EMBEDDING_DIMENSION", "1024"))
    except ValueError:
        return 1024


def get_semantic_match_threshold() -> float:
    try:
        return float(os.getenv("SEMANTIC_MATCH_THRESHOLD", "0.5"))
    except ValueError:
        return 0.5
