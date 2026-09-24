"""Explicit side-effect declarations shared by MCP annotations and discovery.

New tools must be classified deliberately; hints are not authorization controls.
"""
from mcp.types import ToolAnnotations

READ_ONLY_TOOLS = frozenset({
    'archery_describe_table',
    'archery_list_columns',
    'archery_list_databases',
    'archery_list_instances',
    'archery_query',
    'archery_query_tenant',
    'check_marmot_script_static',
    'choerodon_download_attachment',
    'choerodon_get_status_map',
    'choerodon_list_attachments',
    'choerodon_list_comments',
    'choerodon_list_issue',
    'choerodon_list_projects',
    'choerodon_preview_comment',
    'choerodon_query_issue',
    'choerodon_search_tasks_by_person',
    'choerodon_search_users',
    'diagnose_context',
    'es_count',
    'es_get',
    'es_search',
    'get_adapter_script_info',
    'get_adapter_script_source',
    'get_knowledge',
    'get_sql_template',
    'get_standalone_script_info',
    'get_standalone_script_source',
    'get_table',
    'get_table_relations',
    'get_workflow_guide',
    'gitlab_get_file',
    'gitlab_list_branches',
    'gitlab_list_tree',
    'gitlab_search_code',
    'gitlab_search_projects',
    'inspect_object_relation',
    'list_sql_templates',
    'obs_log_datasources',
    'obs_log_query',
    'obs_log_trace',
    'obs_sls_query',
    'obs_sls_targets',
    'query_script_trace',
    'search_adapter_script_source',
    'search_adapter_scripts',
    'search_knowledge',
    'search_pangu',
    'search_repo',
    'search_sql_templates',
    'search_standalone_script_source',
    'search_standalone_scripts',
    'search_tables',
})

WRITE_TOOLS = frozenset({
    'add_table_relation',
    'choerodon_add_comment',
    'choerodon_update_comment',
    'choerodon_delete_comment',
    'delete_knowledge',
    'delete_sql_template',
    'record_table_usage',
    'record_template_usage',
    'save_knowledge',
    'save_sql_template',
    'update_knowledge',
    'update_sql_template',
    'upsert_table_knowledge',
})

# Updates/upserts may replace existing content; additions/statistics are additive.
DESTRUCTIVE_TOOLS = frozenset({
    "choerodon_update_comment", "choerodon_delete_comment",
    "update_knowledge", "delete_knowledge", "update_sql_template",
    "delete_sql_template", "add_table_relation", "upsert_table_knowledge",
})
LOCAL_TOOLS = frozenset({"search_repo", "check_marmot_script_static", "get_workflow_guide", "choerodon_preview_comment"})


def tool_annotations(name: str) -> ToolAnnotations:
    if name not in READ_ONLY_TOOLS | WRITE_TOOLS:
        raise ValueError(f"Missing side-effect policy for MCP tool: {name}")
    readonly = name in READ_ONLY_TOOLS
    return ToolAnnotations(
        readOnlyHint=readonly,
        destructiveHint=name in DESTRUCTIVE_TOOLS,
        # Conservative for all writes, including timestamps, counters and embeddings.
        idempotentHint=readonly,
        openWorldHint=name not in LOCAL_TOOLS,
    )
