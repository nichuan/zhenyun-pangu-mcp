"""Archery 用户 SQL 安全边界测试。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from zhenyun_pangu_mcp.archery import ArcheryError, is_write_sql, validate_select_sql  # noqa: E402


@pytest.mark.parametrize(
    "sql",
    [
        "SHOW DATABASES",
        "DESC hpfm_tenant",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "SELECT tenant_id FROM hpfm_tenant UNION SELECT tenant_id FROM hpfm_tenant",
        "SELECT ROW_NUMBER() OVER (ORDER BY tenant_id) FROM hpfm_tenant",
        "SELECT tenant_id, COUNT(*) FROM hpfm_tenant GROUP BY tenant_id",
        "SELECT (SELECT 1)",
        "SELECT 1; DELETE FROM hpfm_tenant",
        "-- comment\nSELECT tenant_id FROM hpfm_tenant",
        "UPDATE hpfm_tenant SET enabled_flag = 0",
        "DELETE FROM hpfm_tenant",
        "INSERT INTO hpfm_tenant (tenant_num) VALUES ('x')",
    ],
)
def test_validate_select_sql_rejects_unsupported_syntax(sql):
    with pytest.raises(ArcheryError):
        validate_select_sql(sql)
    assert is_write_sql(sql) is True


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT tenant_id, tenant_num FROM hpfm_tenant WHERE tenant_num = 'SRM' LIMIT 10",
        "SELECT tenant_id FROM hpfm_tenant ORDER BY tenant_id DESC LIMIT 10;",
        "SELECT enabled_flag FROM hpfm_tenant WHERE tenant_name LIKE '%采购%'",
    ],
)
def test_validate_select_sql_accepts_basic_queries(sql):
    validate_select_sql(sql)
    assert is_write_sql(sql) is False


@pytest.mark.parametrize(
    "sql",
    [
        "EXPLAIN SELECT tenant_id FROM hpfm_tenant LIMIT 10",
        "SHOW CREATE TABLE hpfm_tenant",
        "SHOW CREATE TABLE `srm`.`hpfm_tenant`;",
    ],
)
def test_validate_select_sql_accepts_read_only_schema_queries(sql):
    validate_select_sql(sql)
    assert is_write_sql(sql) is False


@pytest.mark.parametrize(
    "sql",
    [
        "EXPLAIN UPDATE hpfm_tenant SET enabled_flag = 0",
        "SHOW CREATE TABLE hpfm_tenant extra",
        "SHOW CREATE TABLE",
        "SHOW TABLES",
    ],
)
def test_validate_select_sql_rejects_other_explain_or_show(sql):
    with pytest.raises(ArcheryError):
        validate_select_sql(sql)
