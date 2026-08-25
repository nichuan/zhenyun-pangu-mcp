# zhenyun-pangu-mcp

> ## 架构定位：盘古「实时接口」
>
> 在四层架构中，本 MCP 是 **Knowledge / Skill / Template 之外的唯一"实时事实"来源**：
>
> | 层 | 回答什么 | 载体 |
> |---|---|---|
> | **Skill** | 这个任务应该怎么做 | `custom-skills/` 的 SKILL.md（编排流程） |
> | **Knowledge** | 业务/系统/字段**是什么** | 本 MCP 的 `knowledge_base/`（稳定事实，沉淀于 knowledge_docs） |
> | **Template** | 以前类似问题**怎么解决** | 本 MCP 的 `knowledge_base/`（SQL 模板，沉淀于 sql_templates） |
> | **zhenyun-pangu-mcp** | **现在**生产环境真实**是什么/发生了什么** | 本 MCP（日志 / 数据 / 猪齿鱼 / 代码） |
>
> **边界原则**：
> - 所有**可能变化**的实时事实（当前日志、当前数据、当前 Schema、当前状态、当前服务状态）一律走本 MCP。
> - 静态知识（Skill Markdown / Knowledge / Template）只负责帮助 Agent 理解这些实时数据意味着什么，**不替代实时查询**。
> - 旧的独立 `log-ops` / `sql-ops` / `gitlab-code` MCP 已被本 MCP 整合取代，不再是正式概念，请勿引用。
>
> **能力分类**（供 Agent 理解"何时用哪类工具"）：
> - **认知层（知识/模板/表）**：`search_knowledge` / `get_knowledge` / `search_sql_templates` / `get_sql_template` / `search_tables` / `get_table` / `get_table_relations` / `search_pangu`（统一搜索）/ `diagnose_context`（组合诊断）
> - **日志能力**：`obs_log_query` / `obs_log_trace` / `obs_log_datasources` / `obs_sls_query`
> - **数据能力**：`archery_query` / `archery_describe_table` / `archery_list_columns` / `archery_query_tenant` / `archery_list_databases` / `archery_list_instances`
> - **业务系统能力**：`choerodon_*` 系列（猪齿鱼协作，以只读查询为主，`choerodon_add_comment` 为需确认的写操作）
> - **代码能力**：`gitlab_*`（GitLab 仓库：项目/代码/文件/目录/分支）+ `search_repo`（本地跨仓搜索）
>
> **只读/写边界（安全）**：日志查询、Schema/数据查询、猪齿鱼查询类（`choerodon_*_issue` / `choerodon_list_*` / `choerodon_search_*` / `choerodon_get_*` / `choerodon_download_*`）、代码检索为**只读**，Agent 可自主调用。任何生产 INSERT/UPDATE/DELETE **不在本 MCP 提供**，统一由 Skill 生成 SQL 后交用户人工确认执行。认知层写操作：`save_knowledge` / `save_sql_template` / `update_sql_template` / `list_sql_templates`（写入 knowledge_docs / sql_templates 元数据，默认相似去重），不影响业务数据；其中 `choerodon_add_comment` 与 `update_sql_template` 会真实写入外部系统（猪齿鱼评论 / 模板库），调用前必须向用户确认内容。

甄云盘古通用工具 MCP，供任意 MCP 客户端（Claude Desktop / Cursor / 各类 agent）复用。

**完全自包含**：不依赖任何外部项目目录，仅需在 `.env` 配置真实凭据即可使用。

## 能力总览

工具按前缀/能力分组（共 38 个）：

| 前缀 | 工具 | 说明 |
|------|------|------|
| `knowledge`（认知层） | `search_knowledge` / `get_knowledge` | 业务知识/排查经验：混合检索（语义+关键词）+ 详情（沉淀于 knowledge_docs） |
| `template`（行动层） | `search_sql_templates` / `get_sql_template` / `list_sql_templates` | 可复用 SQL/修复模板：混合检索 + 详情 + 总览（沉淀于 sql_templates） |
| `table`（事实层） | `search_tables` / `get_table` / `get_table_relations` | 表目录 + 关联关系（沉淀于 table_catalog / table_relations） |
| `search_pangu` | `search_pangu` | 统一搜索：一次检索 知识 + 模板 + 表 + 关系 |
| `diagnose_context` | `diagnose_context` | 组合诊断：自动汇集 认知 → 模板 → 表 → 关系 的诊断上下文 |
| `save_*` / `update_*`（知识库写） | `save_knowledge` / `save_sql_template` / `update_sql_template` | 沉淀/更新知识模板（默认相似去重，写入元数据不碰业务数据；`update_sql_template` 为写操作） |
| `obs_*` | `obs_log_query` / `obs_log_trace` / `obs_log_datasources` / `obs_sls_query` | 日志能力：Loki 双平台（aws 海外全环境 + cn 国内非生产）+ 阿里云 SLS（仅 cn 国内盘古 prod） |
| `archery_*` | `archery_query` / `archery_describe_table` / `archery_list_columns` / `archery_query_tenant` / `archery_list_databases` / `archery_list_instances` | 数据能力（Archery 双站点 cn/aws + 盘古专属租户/库/实例能力） |
| `choerodon_*` | `choerodon_query_issue` / `choerodon_list_issue` / `choerodon_search_users` / `choerodon_get_status_map` / `choerodon_search_tasks_by_person` / `choerodon_list_attachments` / `choerodon_download_attachment` / `choerodon_list_comments` / `choerodon_add_comment` | 业务系统能力：猪齿鱼协作（内置 Python 客户端，纯 HTTP 登录；前 7 个为只读查询，`choerodon_add_comment` 为写操作，需确认） |
| `gitlab_*` | `gitlab_search_projects` / `gitlab_search_code` / `gitlab_get_file` / `gitlab_list_tree` / `gitlab_list_branches` | 代码能力：GitLab 仓库（项目 / 代码 / 文件 / 目录 / 分支） |
| `search_repo` | `search_repo` | 代码能力：跨本地代码仓库搜索（内容 / 文件名 / 模块结构） |

## 日志平台区分（重要）

盘古日志分布在三个不同平台，查询前需先确认目标环境落在哪个平台：

| 能力 | 环境 | 平台 | 数据源 / project | 标签体系 |
|------|------|------|------------------|----------|
| `obs_log_query` | AWS 海外（全部环境） | Grafana/Loki | `Jp-saas-1-prod` / `Jp-saas-1-noneprod` / `ops` | `job` / `app` |
| `obs_log_query` | cn 国内非生产（dev/test） | Grafana/Loki | `Loki (pangu-noneprod)` 等 | `namespace` / `service_name` / `pod_name` / `container_name` |
| `obs_sls_query` | **仅 cn 国内盘古 prod** | 阿里云 SLS | `pangu-cn-saas-3-prod-shared-sls-project-0` / `sls-store-0-pangu-prod` | `_namespace_` = `saas-prod` |

- **cn 国内盘古 `prod` 日志 = 阿里云 SLS**，用 `obs_sls_query` 查询。
- **cn 国内盘古 `dev`/`test` 等非生产**用 `obs_log_query(region="cn")`，盘古非生产面板（`namespace=saas-dev-new` / `saas-test-new`）日志在 `Loki (pangu-noneprod)` 数据源。
- **AWS 海外（无论 prod/非生产）**全部用 `obs_log_query(region="aws")`。
- ⚠️ 路由铁律：**除 cn 国内盘古 prod 走 SLS 外，其余（cn 非生产 + 全部 AWS）都走 Loki**。盘古非生产不要调 `obs_sls_query`。

> 三个平台的登录凭据、数据源名均在 `.env` 独立配置，详见 `.env.example`。

## 安装与运行

```bash
cd zhenyun-pangu-mcp
uv sync  # 或 pip install -e .
cp .env.example .env   # 填写真实凭据
```

以 stdio 运行：

```bash
uv run zhenyun-pangu-mcp
# 或
python -m zhenyun_pangu_mcp
```

## MCP 客户端配置

```json
{
  "mcpServers": {
    "zhenyun-pangu-mcp": {
      "command": "uvx",
      "args": ["--from", "/path/to/zhenyun-pangu-mcp", "zhenyun-pangu-mcp"],
      "env": {
        "MCP_ENV_DIR": "/path/to/zhenyun-pangu-mcp"
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
| GitLab | `GITLAB_BASE_URL` / `GITLAB_TOKEN`（或 `GITLAB_USERNAME`/`GITLAB_PASSWORD`） | GitLab 仓库地址与凭据 |
| | `GITLAB_SEARCH_ROOT_ID` / `GITLAB_SEARCH_ROOT_GROUP` | 代码搜索根目录（限定 group/project，避免全站噪声） |
| 其他 | `PG_ROOT` | 本地跨仓搜索根目录（默认本仓库根） |

> 凭据请勿提交 git；`.env` 已由 `.gitignore` 排除，仅 `.env.example`（占位符版）入库。

## 输出与错误规范（Agent 可据此判断）

**成功返回**：各工具返回 JSON 字符串，尽量包含 `summary / total / results` 等摘要 + 关键结果，避免一次性返回数千行吃 Agent Context：
- 日志查询：`obs_log_query` / `obs_log_trace` 返回 `total`（命中总数）+ 截断后的 `results`（按 `limit`），`obs_log_trace` 附 `meta.error_count / warn_count`，可用 `level=error` 省 token。
- 数据查询：`archery_query` 返回行集与数量；查询大结果集建议缩小 `limit` 或用更精准 WHERE。

**失败返回**：工具异常一律返回 `{"error": "<原因>"}` 的 JSON 字符串，不抛 500。常见原因：
- `参数错误`：site/instance/db/query 取值非法（如未知 region）。
- `权限错误`：Archery/Grafana/SLS 凭据缺失、过期或无权。
- `连接错误 / 查询超时`：网络或时间窗过宽（缩小时间范围重试）。
- `数据不存在`：查询无结果。

> Agent 应根据 `error` 字段判断失败原因并调整参数后重试，不要用相同参数原样重调。
