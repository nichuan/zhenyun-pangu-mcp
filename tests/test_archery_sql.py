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
        "SELECT ROW_NUMBER() OVER (ORDER BY tenant_id) FROM hpfm_tenant",
        "SELECT 1; DELETE FROM hpfm_tenant",
        "-- comment\nSELECT tenant_id FROM hpfm_tenant",
        "/*!50000 UPDATE hpfm_tenant SET enabled_flag = 0 */ SELECT 1",
        "SELECT 1 /*org.Mapper.method*/",
        "/*org.Mapper.method*/ SELECT 1; DELETE FROM hpfm_tenant",
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
        "SELECT CASE WHEN enabled_flag = 1 THEN 'enabled' ELSE 'disabled' END AS state FROM hpfm_tenant LIMIT 10",
        "SELECT tenant_id FROM hpfm_tenant WHERE tenant_id IN (1, 2)",
        "SELECT tenant_id FROM hpfm_tenant WHERE tenant_num NOT IN ('SRM', 'SRM-AUX') LIMIT 10",
        "SELECT tenant_id FROM hpfm_tenant WHERE tenant_id IN (SELECT tenant_id FROM hpfm_tenant)",
        "SELECT COUNT(*) FROM (SELECT tenant_id FROM hpfm_tenant) x",
        "SELECT (SELECT 1)",
        "SELECT (1 + 2) AS value",
        "SELECT tenant_id FROM hpfm_tenant UNION ALL SELECT tenant_id FROM hpfm_tenant",
        "/*org.srm.purchasecooperation.pr.infra.mapper.PrLineMapper.sourceNewLinkQuotePrQueryList*/\n"
        "SELECT spl.pr_line_id FROM sprm_pr_line spl LEFT JOIN "
        "(SELECT * FROM sprm_pr_header sph WHERE EXISTS "
        "(SELECT 1 FROM hiam_user_authority hua WHERE hua.tenant_id = 163853 "
        "AND hua.authority_type_code = 'PURCHASE_AGENT' "
        "AND hua.user_id IN (SELECT id FROM iam_user UNION ALL SELECT id FROM iam_user))) sph "
        "ON spl.pr_header_id = sph.pr_header_id WHERE spl.tenant_id = 163853 "
        "AND (spl.closed_flag = 0 OR spl.cancelled_flag = 0) LIMIT 20",
    ],
)
def test_validate_select_sql_accepts_basic_queries(sql):
    validate_select_sql(sql)
    assert is_write_sql(sql) is False


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(*) FROM hpfm_tenant WHERE tenant_num = 'SRM'",
        "SELECT SUM(amount), AVG(amount), MIN(amount), MAX(amount) FROM sodr_order",
        "SELECT IFNULL(NULLIF(tenant_name, ''), 'unknown') FROM hpfm_tenant",
        "SELECT CONCAT_WS(',', tenant_num, tenant_name) FROM hpfm_tenant",
        "SELECT CAST(tenant_id AS CHAR) FROM hpfm_tenant",
    ],
)
def test_validate_select_sql_accepts_safe_functions(sql):
    validate_select_sql(sql)
    assert is_write_sql(sql) is False


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT SLEEP(1) FROM hpfm_tenant",
        "SELECT LOAD_FILE('/etc/passwd') FROM hpfm_tenant",
        "SELECT COALESCE(tenant_name, '') FROM hpfm_tenant",
        "SELECT tenant_id FROM hpfm_tenant WHERE tenant_id IN (1, SLEEP(1))",
        "SELECT GET_LOCK('x', 1)",
        "SELECT LOAD_FILE ('/etc/passwd')",
    ],
)
def test_validate_select_sql_rejects_unknown_functions_and_parentheses(sql):
    with pytest.raises(ArcheryError):
        validate_select_sql(sql)
    assert is_write_sql(sql) is True


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
