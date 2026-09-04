"""Adapter JavaScript discovery, decoding, caching, and focused source reads.

The production tables keep their historical Base64(UTF-16BE) representation.
This module is the boundary that prevents encoded payloads from reaching MCP
callers: repositories read the encoded value, while every public service
method returns metadata or decoded JavaScript only.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from . import archery
from .config import (
    ADAPTER_SCRIPT_CACHE_MAX_ENTRIES,
    ADAPTER_SCRIPT_CACHE_TTL_SECONDS,
    ADAPTER_SCRIPT_DEFAULT_LINES,
    ADAPTER_SCRIPT_MAX_RANGE_CHARS,
    ADAPTER_SCRIPT_MAX_RANGE_LINES,
    ARCHERY_DEFAULT_DB,
)

logger = logging.getLogger(__name__)


class AdapterScriptError(Exception):
    """Base error for adapter-script operations."""


class ScriptNotFoundError(AdapterScriptError):
    """Raised when a requested script line does not exist."""


class ScriptDecodeError(AdapterScriptError):
    """Raised when persisted script content cannot be decoded safely."""


def decode_script_content(content: str | None) -> str:
    """Decode the database representation without exposing it to callers.

    Historical adapter rows store JavaScript as Base64-encoded UTF-16BE. Some
    legacy rows contain one trailing byte; the established storage contract is
    to discard that incomplete UTF-16 code unit before decoding.
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

    if len(raw) % 2:
        logger.warning("adapter_script decode trimmed trailing byte raw_size=%d", len(raw))
        raw = raw[:-1]
    try:
        return raw.decode("utf-16-be")
    except UnicodeDecodeError as exc:
        raise ScriptDecodeError("脚本正文不是合法 UTF-16BE") from exc


def _validate_text(value: str, field: str, max_length: int = 200) -> str:
    value = (value or "").strip()
    if len(value) > max_length:
        raise AdapterScriptError(f"{field} 最长允许 {max_length} 个字符")
    if any(ord(char) < 32 and char not in "\t\r\n" for char in value):
        raise AdapterScriptError(f"{field} 包含不允许的控制字符")
    return value


def _sql_literal(value: str) -> str:
    """Quote an internally generated SQL literal for Archery read queries."""
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _script_id(value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise AdapterScriptError("script_id 必须是正整数") from exc
    if parsed <= 0:
        raise AdapterScriptError("script_id 必须是正整数")
    return parsed


class AdapterScriptRepository:
    """Read-only access to adapter script metadata and encoded source."""

    @staticmethod
    def _connection(site: str, instance: str | None, db: str | None):
        default_instance = "JP-SaaS-1-Prod-RW-8.0" if site == "aws" else "SAAS-SRM-PROD数据库"
        instance_name = archery.resolve_instance(instance, site, default_instance)
        return archery._client(site), instance_name, db or ARCHERY_DEFAULT_DB

    @staticmethod
    def _metadata_columns() -> str:
        return (
            "l.id AS script_id, h.id AS header_id, h.task_code, h.description, "
            "h.apply_tenant_num, h.running_service, h.enabled_flag, h.trustful, "
            "h.script_version, l.object_version_number AS source_version, "
            "l.last_update_date AS source_updated_at, l.script_type, "
            "l.filter AS script_filter, l.priority"
        )

    def search(
        self,
        *,
        tenant: str = "",
        service: str = "",
        query: str = "",
        enabled_only: bool = True,
        site: str = "cn",
        instance: str | None = None,
        db: str | None = None,
        limit: int = 20,
    ) -> tuple[list[dict[str, Any]], float]:
        tenant = _validate_text(tenant, "tenant", 100)
        service = _validate_text(service, "service", 100)
        query = _validate_text(query, "query", 200)
        if not tenant and not service and not query:
            raise AdapterScriptError("tenant、service、query 至少提供一项，禁止无条件扫描脚本表")
        limit = max(1, min(int(limit), 100))

        conditions: list[str] = []
        if tenant:
            conditions.append(f"h.apply_tenant_num = {_sql_literal(tenant)}")
        if service:
            conditions.append(f"h.running_service = {_sql_literal(service)}")
        if query:
            pattern = _sql_literal(f"%{query}%")
            conditions.append(f"h.task_code LIKE {pattern} OR h.description LIKE {pattern}")
        if enabled_only:
            conditions.append("h.enabled_flag = 1")

        sql = (
            f"SELECT {self._metadata_columns()} "
            "FROM sada_adaptor_task_header h "
            "JOIN sada_adaptor_task_line l ON l.header_id = h.id "
            f"WHERE {' AND '.join(f'({item})' if ' OR ' in item else item for item in conditions)} "
            "ORDER BY h.id DESC, l.priority, l.id "
            f"LIMIT {limit}"
        )
        # The generic user SQL validator intentionally rejects parentheses. This
        # fixed internal query is built only from escaped literals and cannot be
        # supplied as arbitrary SQL by an MCP caller.
        client, instance_name, db_name = self._connection(site, instance, db)
        started = time.perf_counter()
        result = client.query(
            sql,
            instance_name,
            db_name,
            limit,
            _internal_allow_non_select=True,
        )
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
            f"SELECT {self._metadata_columns()} "
            "FROM sada_adaptor_task_header h "
            "JOIN sada_adaptor_task_line l ON l.header_id = h.id "
            f"WHERE l.id = {script_id} LIMIT 1"
        )
        client, instance_name, db_name = self._connection(site, instance, db)
        started = time.perf_counter()
        result = client.query(sql, instance_name, db_name, 1)
        elapsed = (time.perf_counter() - started) * 1000
        rows = result.get("rows") or []
        if not rows:
            raise ScriptNotFoundError(f"未找到 script_id={script_id} 的适配器脚本")
        return dict(rows[0]), elapsed

    def get_encoded_source(
        self,
        script_id: int,
        *,
        site: str = "cn",
        instance: str | None = None,
        db: str | None = None,
    ) -> tuple[str, float]:
        script_id = _script_id(script_id)
        sql = (
            "SELECT script_content FROM sada_adaptor_task_line "
            f"WHERE id = {script_id} LIMIT 1"
        )
        client, instance_name, db_name = self._connection(site, instance, db)
        started = time.perf_counter()
        result = client.query(sql, instance_name, db_name, 1)
        elapsed = (time.perf_counter() - started) * 1000
        rows = result.get("rows") or []
        if not rows:
            raise ScriptNotFoundError(f"未找到 script_id={script_id} 的适配器脚本正文")
        content = rows[0].get("script_content")
        if content is None:
            return "", elapsed
        if not isinstance(content, str):
            raise ScriptDecodeError(f"script_id={script_id} 的脚本正文类型异常")
        return content, elapsed


@dataclass(frozen=True)
class CacheEntry:
    source: str
    encoded_size: int
    source_hash: str
    created_at: float


class DecodedScriptCache:
    """Small thread-safe TTL/LRU cache keyed by script id and version."""

    def __init__(self, max_entries: int, ttl_seconds: int):
        self.max_entries = max(1, int(max_entries))
        self.ttl_seconds = max(1, int(ttl_seconds))
        self._items: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = threading.RLock()

    def get(self, key: str) -> CacheEntry | None:
        now = time.monotonic()
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                return None
            if now - entry.created_at >= self.ttl_seconds:
                del self._items[key]
                return None
            self._items.move_to_end(key)
            return entry

    def put(self, key: str, source: str, encoded_size: int) -> CacheEntry:
        entry = CacheEntry(
            source=source,
            encoded_size=encoded_size,
            source_hash=hashlib.sha256(source.encode("utf-8")).hexdigest(),
            created_at=time.monotonic(),
        )
        with self._lock:
            self._items[key] = entry
            self._items.move_to_end(key)
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)
        return entry

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


class AdapterScriptService:
    def __init__(
        self,
        repository: AdapterScriptRepository | None = None,
        cache: DecodedScriptCache | None = None,
    ):
        self.repository = repository or AdapterScriptRepository()
        self.cache = cache or DecodedScriptCache(
            ADAPTER_SCRIPT_CACHE_MAX_ENTRIES,
            ADAPTER_SCRIPT_CACHE_TTL_SECONDS,
        )

    @staticmethod
    def _cache_key(metadata: dict[str, Any], **connection: Any) -> str:
        version = ":".join(str(value) for value in (
            metadata.get("script_version"),
            metadata.get("source_version"),
            metadata.get("source_updated_at"),
        ) if value is not None) or "unversioned"
        site = connection.get("site") or "cn"
        instance = connection.get("instance") or "default"
        db = connection.get("db") or ARCHERY_DEFAULT_DB
        return (
            f"{site}:{instance}:{db}:{metadata.get('script_id')}:{version}"
        )

    def search_scripts(self, **kwargs) -> dict[str, Any]:
        if not any((kwargs.get("tenant"), kwargs.get("service"), kwargs.get("query"))):
            raise AdapterScriptError("tenant、service、query 至少提供一项，禁止无条件扫描脚本表")
        rows, db_ms = self.repository.search(**kwargs)
        return {
            "count": len(rows),
            "scripts": rows,
            "performance": {"db_ms": round(db_ms, 3)},
        }

    def get_info(self, script_id: int, **kwargs) -> dict[str, Any]:
        metadata, db_ms = self.repository.get_metadata(script_id, **kwargs)
        cached = self.cache.get(self._cache_key(metadata, **kwargs))
        result = dict(metadata)
        result.update({
            "language": "javascript",
            "encoding_at_rest": "base64+utf-16-be",
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

    def _load_source(self, script_id: int, **kwargs) -> tuple[dict[str, Any], CacheEntry, dict[str, Any]]:
        total_started = time.perf_counter()
        metadata, metadata_db_ms = self.repository.get_metadata(script_id, **kwargs)
        key = self._cache_key(metadata, **kwargs)
        entry = self.cache.get(key)
        source_db_ms = 0.0
        decode_ms = 0.0
        cache_hit = entry is not None

        if entry is None:
            encoded, source_db_ms = self.repository.get_encoded_source(script_id, **kwargs)
            decode_started = time.perf_counter()
            try:
                source = decode_script_content(encoded)
            except ScriptDecodeError as exc:
                logger.error(
                    "adapter_script decode failed script_id=%s encoded_size=%d error=%s",
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
            "adapter_script script_id=%s metadata_db=%.3fms source_db=%.3fms "
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
            "adapter_script result script_id=%s operation=source prepare=%.3fms total=%.3fms returned_size=%d",
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
            "adapter_script result script_id=%s operation=search prepare=%.3fms total=%.3fms returned_size=%d",
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


service = AdapterScriptService()
