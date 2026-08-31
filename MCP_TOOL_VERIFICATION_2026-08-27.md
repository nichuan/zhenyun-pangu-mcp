# MCP 工具全环境验证进度（2026-08-27）

> 进度/待办记录，非知识库内容。Loki 检索方式仍可能变动，**工具代码暂不修改**，待方式确定后重做。

## 一、已验证通过的能力
- **Archery 数据查询**：cn(prod/prod-ro/dev/test) + aws(aws) 共 5 个环境全部连通成功（SELECT 1 探针，query_time < 0.01s）。
- **obs_log_datasources**：cn / aws 均能列出数据源（Grafana 控制面正常）。
- **obs_sls_query（cn prod 阿里云）**：链路健康，progress=Complete。cn prod 按架构应走阿里云 SLS。
- **obs_log_query（aws prod / aws nonprod）**：用合法带标签 LogQL（如 `{app="srm-gateway"}`）查询成功返回（total=0 仅为探针无命中，链路通）。
- **search_repo / GitLab / 认知层**：均正常，不受 Loki 故障影响。

## 二、当前故障点（基础设施层，非工具 bug）
- **cn 非 prod 的 Loki**：持续 HTTP 502（Bad Gateway），所有 env/query/工具均挂。已用 `{}` 空查询、5~10m 窄窗、不同标签、query/trace 双工具交叉验证，确认是底层 Loki 数据面/网关故障，非 query 写法问题，也不在本 MCP 修复范围。
- **aws ops 的 Loki**：持续 HTTP 500，同上属后端故障。

## 三、obs_log_trace 工具自身的待修复项（代码 bug，暂缓修改）
> 因 Loki 检索方式尚未定稿，暂不修改代码，列此待办。

1. **aws 全环境返回 HTTP 400**：`loki.py` 的 `query_trace`（约行 300-307）强制套 `namespace="..."` 模板，但 `_ns_from_env(platform,env)` 对 aws 返回空串 → 拼出 `namespace=""` 非法选择器 → 400；兜底 `unscoped_full='{} |= tid'` 空选择器在 aws 也被拒 → 400。aws 三个 env(prod/nonprod/ops) 的 trace 全部因此挂，与 Loki 后端无关。
2. **cn prod 误走 Loki**：`obs_log_trace` 无 SLS 分支，cn prod 按架构应走阿里云 SLS，实际却查 Loki（返回 502），查不到正确日志。

### 预期修复方向（待检索方式确定后实施）
- aws 平台不套 namespace：当 `_ns_from_env` 返回空时，改用 `{app="..."} |= "tid"` 或无标签子串，不生成 `namespace=""` / `{}`。
- cn prod 路由到 SLS：env=prod 且 region=cn 时调用 SLS trace 查询，而非 Loki。

## 四、当前排障建议
- cn prod 日志：用 `obs_sls_query(environment="prod")`。
- aws prod / aws nonprod 日志：用 `obs_log_query`（带合法标签）+ `obs_log_trace` 暂不可用（待修）。
- cn 非 prod、aws ops 日志：等 Loki 后端恢复。
- 数据查询、代码检索、认知层不受影响。

---

## 五、2026-08-31 更新：路由已重做，第三节两项均已修复

背景：盘古非生产曾迁移到 Loki（`logs.going-link.net`）后该 Loki 出现故障，非生产日志已**迁回阿里云 SLS**。本次改动：

1. **SLS 路由扩容**：`obs_sls_query` 支持 `environment="prod" | "dev" | "test"`（盘古全环境），并新增
   `obs_sls_targets()` 列出真实映射；时间解析统一为 `_time_bounds`（中英文相对时间 / 自然语言 / 绝对时间），
   未显式指定时间窗且 0 命中时自动扩到最近 24h、72h 重试（`meta.attempted_windows`）。
2. **Loki 收缩到仅 AWS**：`LOKI_PLATFORMS` / `LOKI_DATASOURCES` 移除 cn；`obs_log_*` 传 `region="cn"`
   返回「请改用 `obs_sls_query`」提示；`LOKI_API_BASE_CN` / `CN_LOG_*` / `CN_LOG_DS_*` 配置废弃。
3. **第三节待办 1 已修**：`_ns_from_env` 对 aws 返回空时不再拼 `namespace=""`，直接走「不限定范围」查询；
   空选择器 `{}` 被 Loki 拒绝(400)时自动按 `{app=~".+"}` → `{job=~".+"}` 降级重试
   （可用 `LOKI_ANY_SELECTOR` 指定首选）。⚠️ 待在 AWS 环境实测确认（本机访问
   `logs.jp-saas-1.going-link.net` 存在证书域名不匹配，无法远程探测）。
4. **第三节待办 2 已修（架构层面）**：cn 不再走 Loki 工具，国内盘古 prod/dev/test 的 trace 全部由
   `obs_sls_query(trace_id=...)` 承载。
5. 测试：79 个单测全绿（新增 `tests/test_sls_routing.py` 与选择器降级用例）；已实测盘古 test 环境
   SLS 查询连通（`_namespace_: saas-test-new` 返回真实日志）。
