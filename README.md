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
> **只读/写边界（安全）**：日志查询、Schema/数据查询、猪齿鱼查询类（`choerodon_*_issue` / `choerodon_list_*` / `choerodon_search_*` / `choerodon_get_*` / `choerodon_download_*`）、代码检索为**只读**，Agent 可自主调用。`archery_query` 的用户 SQL 只允许单条基础 `SELECT`、`EXPLAIN SELECT` 或 `SHOW CREATE TABLE`，不支持其它 `SHOW/DESC`、`WITH`、多语句、注释、函数/子查询、窗口函数、集合运算或任何写入语法；实例/库/表结构由专用工具提供。任何生产 INSERT/UPDATE/DELETE **不在本 MCP 提供**，统一由 Skill 生成 SQL 后交用户人工确认执行。认知层的 `search_*` / `get_*` / `diagnose_context` / `list_sql_templates` 为只读；`save_*`、`update_*`、`delete_*`、`add_table_relation`、`upsert_table_knowledge` 和使用统计工具会写入 knowledge_docs / sql_templates / table_catalog / table_relations 元数据，不影响业务数据，调用前应确认沉淀内容。`choerodon_add_comment` 会真实写入猪齿鱼评论，必须先确认内容；评论必须传规范 Markdown，禁止纯文本和原始 HTML，工具会负责 Markdown 渲染。

甄云盘古通用工具 MCP，供任意 MCP 客户端（Claude Desktop / Cursor / 各类 agent）复用。

猪齿鱼评论格式说明：`choerodon_add_comment` 接收规范 Markdown，但接口写入的
`commentText` 是统一渲染后的 HTML 富文本。Markdown 表格会转换为 `<table>`，代码块
会转换为 `<pre><code class="language-xxx">`；因此从评论区复制代码时不会带回 Markdown
的 ``` 围栏，这是浏览器复制 HTML 内容的正常表现。不要在同一条评论中手工拼接 HTML 和
Markdown，否则编辑器二次解析时可能出现表格或代码块样式互相覆盖。

**完全自包含**：不依赖任何外部项目目录，仅需在 `.env` 配置真实凭据即可使用。

## 能力总览

工具按前缀/能力分组（共 43 个）：

| 前缀 | 工具 | 说明 |
|------|------|------|
| `knowledge`（认知层） | `search_knowledge` / `get_knowledge` | 业务知识/排查经验：混合检索（语义+关键词）+ 详情（沉淀于 knowledge_docs） |
| `template`（行动层） | `search_sql_templates` / `get_sql_template` / `list_sql_templates` | 可复用 SQL/修复模板：混合检索 + 详情 + 总览（沉淀于 sql_templates） |
| `table`（事实层） | `search_tables` / `get_table` / `get_table_relations` | 表目录 + 关联关系（沉淀于 table_catalog / table_relations） |
| `search_pangu` | `search_pangu` | 统一搜索：一次检索 知识 + 模板 + 表 + 关系 |
| `diagnose_context` | `diagnose_context` | 组合诊断：自动汇集 认知 → 模板 → 表 → 关系 的诊断上下文 |
| 知识库维护写操作 | `save_knowledge` / `save_sql_template` / `list_sql_templates` / `update_sql_template` / `delete_sql_template` / `record_template_usage` / `add_table_relation` / `record_table_usage` / `upsert_table_knowledge` | 沉淀、维护知识/模板/表目录/关联关系；仅写认知层元数据，不修改业务库 |
| `obs_*` | `obs_log_query` / `obs_log_trace` / `obs_log_datasources` / `obs_sls_query` | 日志能力：Loki 双平台（aws 海外全环境 + cn 国内非生产）+ 阿里云 SLS（仅 cn 国内盘古 prod） |
| `archery_*` | `archery_query` / `archery_describe_table` / `archery_list_columns` / `archery_query_tenant` / `archery_list_databases` / `archery_list_instances` | 数据能力（Archery 双站点 cn/aws + 盘古专属租户/库/实例能力） |
| `choerodon_*` | `choerodon_query_issue` / `choerodon_list_issue` / `choerodon_search_users` / `choerodon_get_status_map` / `choerodon_search_tasks_by_person` / `choerodon_list_attachments` / `choerodon_download_attachment` / `choerodon_list_comments` / `choerodon_add_comment` | 业务系统能力：猪齿鱼协作（内置 Python 客户端，纯 HTTP 登录；前 8 个为只读查询，`choerodon_add_comment` 为写操作，需确认） |
| `gitlab_*` | `gitlab_search_projects` / `gitlab_search_code` / `gitlab_get_file` / `gitlab_list_tree` / `gitlab_list_branches` | 代码能力：GitLab 仓库（项目 / 代码 / 文件 / 目录 / 分支） |
| `search_repo` | `search_repo` | 代码能力：跨本地代码仓库搜索（内容 / 文件名 / 模块结构） |

## 知识库工具使用指南

认知层存放的是可复用的稳定知识和目录元数据，不是生产实时事实。调用顺序按问题类型选择：

| 你要解决的问题 | 首选工具 | 下一步 |
|---|---|---|
| 不知道某个业务规则、状态、机制是否已有结论 | `search_knowledge` | 用结果 `id` 调 `get_knowledge`；没有命中且结论已确认时再 `save_knowledge` |
| 处理排障或复杂 SQL，尚不清楚要查什么 | `diagnose_context` | 按返回的知识/模板/表/关系，分别调用专项工具和实时日志/Archery |
| 不知道真实表名或业务描述对应哪些表 | `search_tables` | 用表名调 `get_table`/`get_table_relations`，字段存在性再调 Archery |
| 想复用以前的查询/修复方案 | `search_sql_templates` | 用模板 `id` 调 `get_sql_template`；实际复用后调 `record_template_usage` |
| 只知道一句跨域关键词，想快速发现线索 | `search_pangu` | 这是关键词发现，不替代专项检索和实时数据查询 |

认知层与实时事实的边界：`search_knowledge`/`get_knowledge` 回答“业务和机制是什么”；
`search_sql_templates`/`get_sql_template` 回答“以前怎么处理”；`search_tables`/`get_table`/
`get_table_relations` 提供目录注释和已沉淀关系。当前日志、数据、DDL、字段存在性必须分别使用
`obs_*`、`archery_query`、`archery_describe_table`/`archery_list_columns`，不能仅凭知识库内容下结论。

知识库写工具会修改 Supabase 认知层元数据，不会执行模板 SQL，也不会修改业务数据库；除统计工具外，
调用前应先向用户确认要写入的内容：

- `save_knowledge(title, content_md, ...)`：沉淀已确认的规则、机制、排查结论或数据模型说明。
  `content_md` 使用规范 Markdown；`core_tables`、`tags`、`related_template_ids` 为逗号分隔字符串；
  默认 `status=draft`，核验后再标 `verified`。
- `save_sql_template(title, category, scenario, sql_text, ...)`：沉淀复杂查询或供人工确认的修复方案。
  `sql_text` 只写入模板库，不会被 MCP 执行；`parameters` 必须是 JSON 对象字符串，例如
  `{"tenant_id":{"type":"bigint","required":true}}`。
- `list_sql_templates(...)`：只读总览；`update_sql_template(id, ...)`：只修改明确传入的字段，
  适合纠正模板或补充验证标记；`delete_sql_template(id)` 是破坏性维护操作，必须明确确认。
- `add_table_relation(...)`：仅在 Archery/SELECT 验证 join 后沉淀关系，`join_on` 例如
  `a.order_id = b.order_id`，`confidence` 为 0~1；`upsert_table_knowledge(...)`：在 Archery
  确认表真实存在后补录/修正目录描述和标签。两者都不创建外键、不改业务表。
- `record_template_usage(id)` 和 `record_table_usage("a,b")`：仅在实际复用模板/使用表后记录统计，
  不要为了提高排序而虚增计数。

推荐的最小工作流：

```text
问题/排障 → diagnose_context 或 search_knowledge
          → get_knowledge / search_sql_templates / search_tables
          → Archery / 日志工具确认当前事实
          → 输出结果；确认后才 save_knowledge/save_sql_template
          → 实际复用后 record_template_usage/record_table_usage
```

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
uv run python -m zhenyun_pangu_mcp
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
