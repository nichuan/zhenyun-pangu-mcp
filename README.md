# zhenyun-pangun-mcp

甄云盘古通用工具 MCP，供任意 MCP 客户端（Claude Desktop / Cursor / 各类 agent）复用。

**完全自包含**：不依赖任何外部项目目录，仅需在 `.env` 配置真实凭据即可使用。

## 能力总览

工具按前缀分组（共 17 个）：

| 前缀 | 工具 | 说明 |
|------|------|------|
| `obs_*` | `obs_log_query` / `obs_log_datasources` / `obs_sls_query` | 日志查询：Loki 双平台（aws 海外全环境 + cn 国内非生产）+ 阿里云 SLS（仅 cn 国内盘古 prod） |
| `archery_*` | `archery_query` / `archery_describe_table` / `archery_list_columns` / `archery_query_tenant` / `archery_list_databases` / `archery_list_instances` | 数据库查询（Archery 双站点 cn/aws + 盘古专属租户/库/实例能力） |
| `choerodon_*` | `choerodon_query_issue` / `choerodon_list_issue` / `choerodon_search_users` / `choerodon_get_status_map` / `choerodon_search_tasks_by_person` / `choerodon_list_attachments` / `choerodon_download_attachment` | 猪齿鱼协作（内置 Python 客户端，纯 HTTP 登录） |
| `search_repo` | `search_repo` | 跨本地代码仓库搜索（内容 / 文件名 / 模块结构） |

## 日志平台区分（重要）

盘古日志分布在三个不同平台，查询前需先确认目标环境落在哪个平台：

| 能力 | 环境 | 平台 | 数据源 / project | 标签体系 |
|------|------|------|------------------|----------|
| `obs_log_query` | AWS 海外（全部环境） | Grafana/Loki | `Jp-saas-1-prod` / `Jp-saas-1-noneprod` / `ops` | `job` / `app` |
| `obs_log_query` | cn 国内非生产（dev/test） | Grafana/Loki | `Loki (pangu-noneprod)` 等 | `namespace` / `service_name` / `pod_name` / `container_name` |
| `obs_sls_query` | **仅 cn 国内盘古 prod** | 阿里云 SLS | `pangu-cn-saas-3-prod-shared-sls-project-0` / `sls-store-0-pangu-prod` | `_namespace_` = `saas-prod` |

- **cn 国内盘古 `prod` 日志 = 阿里云 SLS**（对接方式复刻自 `log-ops-mcp`），用 `obs_sls_query` 查询。
- **cn 国内盘古 `dev`/`test` 等非生产**用 `obs_log_query(region="cn")`，盘古非生产面板（`namespace=saas-dev-new` / `saas-test-new`）日志在 `Loki (pangu-noneprod)` 数据源。
- **AWS 海外（无论 prod/非生产）**全部用 `obs_log_query(region="aws")`。
- ⚠️ 路由铁律：**除 cn 国内盘古 prod 走 SLS 外，其余（cn 非生产 + 全部 AWS）都走 Loki**。盘古非生产不要调 `obs_sls_query`。

> 三个平台的登录凭据、数据源名均在 `.env` 独立配置，详见 `.env.example`。

## 安装与运行

```bash
cd zhenyun-pangun-mcp
uv sync  # 或 pip install -e .
cp .env.example .env   # 填写真实凭据
```

以 stdio 运行：

```bash
uv run zhenyun-pangun-mcp
# 或
python -m zhenyun_pangun_mcp
```

## MCP 客户端配置

```json
{
  "mcpServers": {
    "zhenyun-pangun-mcp": {
      "command": "uvx",
      "args": ["--from", "/path/to/zhenyun-pangun-mcp", "zhenyun-pangun-mcp"],
      "env": {
        "MCP_ENV_DIR": "/path/to/zhenyun-pangun-mcp"
      }
    }
  }
}
```

## 配置项（`.env`）

| 分组 | 环境变量 | 说明 |
|------|----------|------|
| Archery | `ARCHERY_USERNAME` / `ARCHERY_PASSWORD` / `ARCHERY_AWS_USERNAME` / `ARCHERY_AWS_PASSWORD` | 数据库网关 cn/aws 凭据 |
| | `ARCHERY_DB_CN` / `ARCHERY_DB_AWS` / `ARCHERY_DB_DEV` / `ARCHERY_DB_TEST` | 实例别名 → 真实实例名 |
| Loki | `AWS_LOG_USERNAME` / `AWS_LOG_PASSWORD` / `CN_LOG_USERNAME` / `CN_LOG_PASSWORD` | Grafana 登录凭据 |
| | `AWS_LOG_DS_*` / `CN_LOG_DS_*` | 环境 → 数据源名映射 |
| Choerodon | `CHOERODON_BASE_URL` / `CHOERODON_USERNAME` / `CHOERODON_PASSWORD` | 猪齿鱼网关与登录凭据 |
| SLS | `SLS_PANGU_PROD_ACCESS_KEY_ID` / `SLS_PANGU_PROD_ACCESS_KEY_SECRET` | 盘古正式环境阿里云日志凭据 |
| 其他 | `PG_ROOT` | 跨仓搜索根目录（默认本仓库根） |

> 凭据请勿提交 git；`.env` 已由 `.gitignore` 排除，仅 `.env.example`（占位符版）入库。
