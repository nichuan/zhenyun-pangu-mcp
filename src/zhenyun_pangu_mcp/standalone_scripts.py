"""Standalone (Marmot) script discovery, decoding, caching, and source reads.

Standalone scripts (Marmot script library) have NO dedicated physical table:
every row lives in the rel-table wide table ``spfm_rel_table_record`` with
``table_code = 'marmot_script_library'``. Slot mapping (verified on 2026-09-05):

    value1     -> type flag (1/2)
    value2     -> apply tenant num (tenant_id is always 0; filter by value2!)
    value3     -> script code (task_code)
    value4     -> description
    value5     -> content kind (e.g. ``template``)
    longValue1 -> script/template body (Base64; encoding varies per row)

This module is the boundary that prevents encoded payloads from reaching MCP
callers: repositories read the encoded value, while every public service method
returns metadata or decoded text only.
"""
from __future__ import annotations

import base64
import binascii
import logging
import time
from typing import Any

from . import archery
from .adapter_scripts import (
    ADAPTER_SCRIPT_CACHE_TTL_SECONDS,
    ADAPTER_SCRIPT_DEFAULT_LINES,
    ADAPTER_SCRIPT_MAX_RANGE_CHARS,
    ADAPTER_SCRIPT_MAX_RANGE_LINES,
    ARCHERY_DEFAULT_DB,
    AdapterScriptError,
    DecodedScriptCache,
    ScriptDecodeError,
    ScriptNotFoundError,
    _sql_literal,
    _validate_text,
    _script_id,
)
from .config import ADAPTER_SCRIPT_CACHE_MAX_ENTRIES

logger = logging.getLogger(__name__)

#: rel-table ``table_code`` that hosts the standalone Marmot script library.
SCRIPT_LIBRARY_TABLE_CODE = "marmot_script_library"

#: longValue slots probed (in order) for the script/template body.
_CONTENT_COLUMNS = ("longValue1", "longValue2", "longValue3", "longValue", "longValue4", "longValue5")

_METADATA_COLUMNS = (
    "id AS script_id, value1 AS type_flag, value2 AS tenant_num, "
    "value3 AS task_code, value4 AS description, value5 AS content_kind, "
    "creation_date, last_update_date AS source_updated_at"
)


def _looks_like_text(text: str) -> bool:
    """Heuristic check that a decoding candidate yields readable script text.

    JS/JSON bodies are dominated by ASCII syntax characters, whitespace and
    digits. A wrong-endianness UTF-16 decoding of ASCII bytes yields high-plane
    CJK garbage that is technically "printable", so the printable ratio alone
    cannot discriminate. We therefore require a minimum ASCII-like ratio while
    still accepting legitimately Chinese-heavy comments (mixed in with code).
    """
    if not text:
        return False
    total = len(text)
    ascii_like = sum(
        1 for char in text
        if (ord(char) < 128 and char.isprintable()) or char in "\t\r\n"
    )
    return ascii_like / total >= 0.3


def decode_script_content(content: str | None) -> str:
    """Decode a Base64 slot body, detecting the per-row text encoding.

    Sampled rows store UTF-16LE JSON templates, while other rows may hold
    UTF-16BE JavaScript or plain UTF-8. Decode Base64 then pick the encoding
    that produces readable text (highest printable ratio wins on ties).
    """
    if content is None or content == "":
        return ""
    if not isinstance(content, str):
        raise ScriptDecodeError("脚本正文必须是 Base64 字符串")

    compact = "".join(content.split())
    if not compact:
        return ""
    try:
        raw = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ScriptDecodeError("脚本正文不是合法 Base64") from exc

    if not raw:
        return ""
    if len(raw) % 2:
        logger.warning("standalone_script decode trimmed trailing byte raw_size=%d", len(raw))
        raw_utf16 = raw[:-1]
    else:
        raw_utf16 = raw

    best: tuple[float, float, str] | None = None
    for encoding, payload in (
        ("utf-16-le", raw_utf16),
        ("utf-16-be", raw_utf16),
        ("utf-8", raw),
    ):
        try:
            text = payload.decode(encoding)
        except UnicodeDecodeError:
            continue
        if not _looks_like_text(text):
            continue
        total = len(text)
        ascii_like = sum(
            1 for char in text
            if (ord(char) < 128 and char.isprintable()) or char in "\t\r\n"
        ) / total
        printable = sum(1 for char in text if char.isprintable() or char in "\t\r\n") / total
        # Primary key: ASCII-like ratio (real code beats wrong-endianness
        # garbage); tie-break on printable ratio, then decode order.
        if best is None or (ascii_like, printable) > best[:2]:
            best = (ascii_like, printable, text)
    if best is None:
        raise ScriptDecodeError("脚本正文无法按 UTF-16LE/UTF-16BE/UTF-8 解码为可读文本")
    return best[2]


def _content_column_expression() -> str:
    return ", ".join(_CONTENT_COLUMNS)


class StandaloneScriptRepository:
    """Read-only access to standalone script metadata and encoded source."""

    @staticmethod
    def _connection(site: str, instance: str | None, db: str | None):
        default_instance = "JP-SaaS-1-Prod-RW-8.0" if site == "aws" else "SAAS-SRM-PROD数据库"
        instance_name = archery.resolve_instance(instance, site, default_instance)
        return archery._client(site), instance_name, db or ARCHERY_DEFAULT_DB

    def search(
        self,
        *,
        tenant: str = "",
        query: str = "",
        site: str = "cn",
        instance: str | None = None,
        db: str | None = None,
        limit: int = 20,
    ) -> tuple[list[dict[str, Any]], float]:
        tenant = _validate_text(tenant, "tenant", 100)
        query = _validate_text(query, "query", 200)
        if not tenant and not query:
            raise AdapterScriptError("tenant、query 至少提供一项，禁止无条件扫描脚本库")
        limit = max(1, min(int(limit), 100))

        conditions = [f"table_code = {_sql_literal(SCRIPT_LIBRARY_TABLE_CODE)}"]
        if tenant:
            conditions.append(f"value2 = {_sql_literal(tenant)}")
        if query:
            pattern = _sql_literal(f"%{query}%")
            conditions.append(f"value3 LIKE {pattern} OR value4 LIKE {pattern}")

        sql = (
            f"SELECT {_METADATA_COLUMNS} "
            "FROM spfm_rel_table_record "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY id DESC "
            f"LIMIT {limit}"
        )
        client, instance_name, db_name = self._connection(site, instance, db)
        started = time.perf_counter()
        result = client.query(sql, instance_name, db_name, limit)
        return list(result.get("rows") or []), (time.perf_counter() - started) * 1000

    def get_metadata(
        self,
        script_id: int,
        *,
        site: str = "cn",
        instance: str | None = None,
        db: str | None = None,
    ) -> tuple[dict[str, Any], float]:
        script_id = _script_id(script_id)
        sql = (
            f"SELECT {_METADATA_COLUMNS} "
            "FROM spfm_rel_table_record "
            f"WHERE table_code = {_sql_literal(SCRIPT_LIBRARY_TABLE_CODE)} "
            f"AND id = {script_id} LIMIT 1"
        )
        client, instance_name, db_name = self._connection(site, instance, db)
        started = time.perf_counter()
        result = client.query(sql, instance_name, db_name, 1)
        elapsed = (time.perf_counter() - started) * 1000
        rows = result.get("rows") or []
        if not rows:
            raise ScriptNotFoundError(f"未找到 script_id={script_id} 的独立脚本")
        return dict(rows[0]), elapsed

    def get_encoded_source(
        self,
        script_id: int,
        *,
        site: str = "cn",
        instance: str | None = None,
        db: str | None = None,
    ) -> tuple[str, str | None, float]:
        """Return ``(encoded_content, content_kind, elapsed_ms)``."""
        script_id = _script_id(script_id)
        sql = (
            f"SELECT {_content_column_expression()} "
            "FROM spfm_rel_table_record "
            f"WHERE table_code = {_sql_literal(SCRIPT_LIBRARY_TABLE_CODE)} "
            f"AND id = {script_id} LIMIT 1"
        )
        client, instance_name, db_name = self._connection(site, instance, db)
        started = time.perf_counter()
        result = client.query(sql, instance_name, db_name, 1)
        elapsed = (time.perf_counter() - started) * 1000
        rows = result.get("rows") or []
        if not rows:
            raise ScriptNotFoundError(f"未找到 script_id={script_id} 的独立脚本正文")
        content = ""
        for column in _CONTENT_COLUMNS:
            value = rows[0].get(column)
            if isinstance(value, str) and value.strip():
                content = value
                break
        return content, None, elapsed


class StandaloneScriptService:
    def __init__(
        self,
        repository: StandaloneScriptRepository | None = None,
        cache: DecodedScriptCache | None = None,
    ):
        self.repository = repository or StandaloneScriptRepository()
        self.cache = cache or DecodedScriptCache(
            ADAPTER_SCRIPT_CACHE_MAX_ENTRIES,
            ADAPTER_SCRIPT_CACHE_TTL_SECONDS,
        )

    @staticmethod
    def _cache_key(metadata: dict[str, Any], **connection: Any) -> str:
        version = ":".join(str(value) for value in (
            metadata.get("source_updated_at"),
            metadata.get("object_version_number"),
        ) if value is not None) or "unversioned"
        site = connection.get("site") or "cn"
        instance = connection.get("instance") or "default"
        db = connection.get("db") or ARCHERY_DEFAULT_DB
        return f"{site}:{instance}:{db}:{metadata.get('script_id')}:{version}"

    def search_scripts(self, **kwargs) -> dict[str, Any]:
        if not any((kwargs.get("tenant"), kwargs.get("query"))):
            raise AdapterScriptError("tenant、query 至少提供一项，禁止无条件扫描脚本库")
        rows, db_ms = self.repository.search(**kwargs)
        return {
            "count": len(rows),
            "scripts": rows,
            "storage": {
                "table": "spfm_rel_table_record",
                "table_code": SCRIPT_LIBRARY_TABLE_CODE,
                "note": "独立脚本存于 rel-table 宽表，租户过滤用 value2（tenant_id 恒为 0）",
            },
            "performance": {"db_ms": round(db_ms, 3)},
        }

    def get_info(self, script_id: int, **kwargs) -> dict[str, Any]:
        metadata, db_ms = self.repository.get_metadata(script_id, **kwargs)
        cached = self.cache.get(self._cache_key(metadata, **kwargs))
        result = dict(metadata)
        result.update({
            "language": "javascript",
            "encoding_at_rest": "base64 (utf-16-le/utf-16-be/utf-8 per row)",
            "source_cached": cached is not None,
            "cache_ttl_seconds": self.cache.ttl_seconds,
            "performance": {"db_ms": round(db_ms, 3)},
        })
        if cached is not None:
            result.update({
                "encoded_size": cached.encoded_size,
                "source_length": len(cached.source),
                "total_lines": len(cached.source.splitlines()),
                "source_hash": cached.source_hash,
            })
        return result

    def _load_source(self, script_id: int, **kwargs) -> tuple[dict[str, Any], Any, dict[str, Any]]:
        total_started = time.perf_counter()
        metadata, metadata_db_ms = self.repository.get_metadata(script_id, **kwargs)
        key = self._cache_key(metadata, **kwargs)
        entry = self.cache.get(key)
        source_db_ms = 0.0
        decode_ms = 0.0
        cache_hit = entry is not None

        if entry is None:
            encoded, _, source_db_ms = self.repository.get_encoded_source(script_id, **kwargs)
            decode_started = time.perf_counter()
            try:
                source = decode_script_content(encoded)
            except ScriptDecodeError as exc:
                logger.error(
                    "standalone_script decode failed script_id=%s encoded_size=%d error=%s",
                    script_id,
                    len(encoded),
                    type(exc).__name__,
                )
                raise
            decode_ms = (time.perf_counter() - decode_started) * 1000
            entry = self.cache.put(key, source, len(encoded))

        total_ms = (time.perf_counter() - total_started) * 1000
        performance = {
            "metadata_db_ms": round(metadata_db_ms, 3),
            "source_db_ms": round(source_db_ms, 3),
            "decode_ms": round(decode_ms, 3),
            "total_ms": round(total_ms, 3),
            "cache": "hit" if cache_hit else "miss",
        }
        logger.info(
            "standalone_script script_id=%s metadata_db=%.3fms source_db=%.3fms "
            "decode=%.3fms total=%.3fms cache=%s decoded_size=%d",
            script_id,
            metadata_db_ms,
            source_db_ms,
            decode_ms,
            total_ms,
            performance["cache"],
            len(entry.source),
        )
        return metadata, entry, performance

    def get_source(
        self,
        script_id: int,
        *,
        start_line: int = 1,
        end_line: int = 0,
        full: bool = False,
        **kwargs,
    ) -> dict[str, Any]:
        operation_started = time.perf_counter()
        metadata, entry, performance = self._load_source(script_id, **kwargs)
        source = entry.source
        lines = source.splitlines()
        total_lines = len(lines)

        if full:
            selected = source
            actual_start = 1 if total_lines else 0
            actual_end = total_lines
        elif total_lines == 0:
            selected = ""
            actual_start = actual_end = 0
        else:
            try:
                start_line = int(start_line)
                end_line = int(end_line)
            except (TypeError, ValueError) as exc:
                raise AdapterScriptError("start_line/end_line 必须是整数") from exc
            if start_line < 1:
                raise AdapterScriptError("start_line 必须从 1 开始")
            if start_line > total_lines:
                raise AdapterScriptError(f"start_line 超出脚本总行数 {total_lines}")
            requested_end = end_line or min(total_lines, start_line + ADAPTER_SCRIPT_DEFAULT_LINES - 1)
            if requested_end < start_line:
                raise AdapterScriptError("end_line 不能小于 start_line")
            requested_end = min(requested_end, total_lines)
            if requested_end - start_line + 1 > ADAPTER_SCRIPT_MAX_RANGE_LINES:
                raise AdapterScriptError(
                    f"单次最多读取 {ADAPTER_SCRIPT_MAX_RANGE_LINES} 行，请缩小范围或显式 full=true"
                )
            actual_start = start_line
            actual_end = requested_end
            selected = "\n".join(lines[actual_start - 1 : actual_end])
            if len(selected) > ADAPTER_SCRIPT_MAX_RANGE_CHARS:
                raise AdapterScriptError(
                    f"所选区间超过 {ADAPTER_SCRIPT_MAX_RANGE_CHARS} 字符，请缩小行号范围"
                )

        operation_total_ms = (time.perf_counter() - operation_started) * 1000
        performance["result_prepare_ms"] = round(
            max(0.0, operation_total_ms - performance["total_ms"]), 3
        )
        performance["total_ms"] = round(operation_total_ms, 3)
        logger.info(
            "standalone_script result script_id=%s operation=source prepare=%.3fms total=%.3fms returned_size=%d",
            script_id,
            performance["result_prepare_ms"],
            performance["total_ms"],
            len(selected),
        )
        return {
            **metadata,
            "language": "javascript",
            "start_line": actual_start,
            "end_line": actual_end,
            "total_lines": total_lines,
            "encoded_size": entry.encoded_size,
            "source_length": len(source),
            "source_hash": entry.source_hash,
            "source": selected,
            "performance": performance,
        }

    def search_source(
        self,
        script_id: int,
        query: str,
        *,
        context_lines: int = 10,
        max_matches: int = 20,
        regex: bool = False,
        case_sensitive: bool = False,
        **kwargs,
    ) -> dict[str, Any]:
        import re

        operation_started = time.perf_counter()
        query = _validate_text(query, "query", 500)
        if not query:
            raise AdapterScriptError("query 不能为空")
        context_lines = max(0, min(int(context_lines), 50))
        max_matches = max(1, min(int(max_matches), 50))
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            pattern = re.compile(query if regex else re.escape(query), flags)
        except re.error as exc:
            raise AdapterScriptError(f"非法正则表达式: {exc}") from exc

        metadata, entry, performance = self._load_source(script_id, **kwargs)
        lines = entry.source.splitlines()
        matches: list[dict[str, Any]] = []
        returned_chars = 0
        truncated = False
        for index, line in enumerate(lines):
            if not pattern.search(line):
                continue
            start = max(1, index + 1 - context_lines)
            end = min(len(lines), index + 1 + context_lines)
            snippet = "\n".join(lines[start - 1 : end])
            if returned_chars + len(snippet) > ADAPTER_SCRIPT_MAX_RANGE_CHARS:
                truncated = True
                break
            matches.append({
                "line": index + 1,
                "start_line": start,
                "end_line": end,
                "source": snippet,
            })
            returned_chars += len(snippet)
            if len(matches) >= max_matches:
                truncated = any(pattern.search(rest) for rest in lines[index + 1 :])
                break

        operation_total_ms = (time.perf_counter() - operation_started) * 1000
        performance["result_prepare_ms"] = round(
            max(0.0, operation_total_ms - performance["total_ms"]), 3
        )
        performance["total_ms"] = round(operation_total_ms, 3)
        logger.info(
            "standalone_script result script_id=%s operation=search prepare=%.3fms total=%.3fms returned_size=%d",
            script_id,
            performance["result_prepare_ms"],
            performance["total_ms"],
            returned_chars,
        )
        return {
            **metadata,
            "language": "javascript",
            "query": query,
            "match_count": len(matches),
            "matches": matches,
            "total_lines": len(lines),
            "truncated": truncated,
            "performance": performance,
        }


service = StandaloneScriptService()
