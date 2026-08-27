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
