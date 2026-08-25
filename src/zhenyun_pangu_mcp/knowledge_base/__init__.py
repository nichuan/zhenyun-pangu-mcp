"""knowledge_base —— zhenyun-pangu-mcp 的「认知层」能力子包。

聚合：
  repository  四张表（knowledge_docs/sql_templates/table_catalog/table_relations）的读写检索
  service     Agent 面向的语义化工具实现（混合检索 / 详情 / 统一搜索 / 诊断上下文）
"""
from . import repository, service

__all__ = ["repository", "service"]
