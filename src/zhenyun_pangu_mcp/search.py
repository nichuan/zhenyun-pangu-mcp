"""跨仓代码搜索(完全自包含,无外部仓库依赖)。

将原外部脚本 pg-repo-search.py 的逻辑内联到本 MCP,使用纯标准库实现
"内容搜索 / 文件名搜索 / 模块列表" 三种能力,避免 spawn 外部进程。
默认扫描根目录由配置 PG_ROOT 指定(默认回退到本包所在仓库根)。
"""

from __future__ import annotations

import os
import re

# 搜索时跳过的目录名(与上游脚本保持一致)
EXCLUDE_DIRS = {
    "node_modules", ".git", ".svn", "dist", "build", "target",
    ".next", ".cache", "__pycache__", ".venv", "venv",
}
# 默认为二进制的扩展名(内容搜索跳过,避免乱码)
SKIP_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".bmp", ".webp",
    ".zip", ".tar", ".gz", ".rar", ".7z", ".pdf", ".doc", ".docx",
    ".xls", ".xlsx", ".ppt", ".pptx", ".exe", ".dll", ".so", ".dylib",
    ".class", ".jar", ".wasm", ".woff", ".woff2", ".ttf", ".eot",
}

DEFAULT_MAX = 30
DEFAULT_CONTEXT = 2
DEFAULT_DEPTH = 4


def _iter_files(root: str):
    """遍历 root 下所有文件,跳过排除目录/二进制文件。

    Yields (rel_path, abs_path)。
    """
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        # 原地剪枝,避免进入无关目录
        dirnames[:] = [
            d for d in dirnames
            if d not in EXCLUDE_DIRS and not d.startswith(".")
        ]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext in SKIP_EXT:
                continue
            abs_path = os.path.join(dirpath, fn)
            rel = os.path.relpath(abs_path, root)
            yield rel, abs_path


def _split_rel(rel: str) -> dict:
    """从相对路径解析出 service / module / layer 元组,用于结果分组展示。"""
    parts = rel.split(os.sep)
    return {
        "service": parts[0] if parts else "",
        "module": parts[1] if len(parts) > 1 else "",
        "layer": parts[2] if len(parts) > 2 else "",
    }


def _read_lines(abs_path: str):
    """读取文件为行列表,失败返回 None。"""
    try:
        with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read().splitlines()
    except Exception:
        return None


def search_content(keyword: str, root: str, max_results: int = DEFAULT_MAX,
                   context: int = DEFAULT_CONTEXT, depth: int = DEFAULT_DEPTH):
    """内容搜索:在 root 下所有文本文件中查找 keyword。"""
    pattern = re.compile(re.escape(keyword), re.IGNORECASE)
    hits: list[dict] = []
    for rel, abs_path in _iter_files(root):
        # 深度过滤(仅对内容搜索生效,对应上游 --depth)
        if len(rel.split(os.sep)) > depth + 1:
            continue
        lines = _read_lines(abs_path)
        if lines is None:
            continue
        for i, line in enumerate(lines):
            if pattern.search(line):
                start = max(0, i - context)
                end = min(len(lines), i + context + 1)
                snippet = "\n".join(lines[start:end])
                meta = _split_rel(rel)
                hits.append({
                    "file": rel,
                    "line": i + 1,
                    "match": keyword,
                    "snippet": snippet,
                    "service": meta["service"],
                    "module": meta["module"],
                    "layer": meta["layer"],
                })
                if len(hits) >= max_results:
                    return hits
    return hits


def search_filename(keyword: str, root: str, max_results: int = DEFAULT_MAX,
                    depth: int = DEFAULT_DEPTH):
    """文件名搜索:按文件名(含路径)模糊匹配 keyword。"""
    pattern = re.compile(re.escape(keyword), re.IGNORECASE)
    hits: list[dict] = []
    for rel, _abs_path in _iter_files(root):
        if len(rel.split(os.sep)) > depth + 1:
            continue
        if pattern.search(rel):
            meta = _split_rel(rel)
            hits.append({
                "file": rel,
                "service": meta["service"],
                "module": meta["module"],
                "layer": meta["layer"],
            })
            if len(hits) >= max_results:
                return hits
    return hits


def list_modules(root: str, depth: int = DEFAULT_DEPTH):
    """模块列表:列出 root 下的服务/模块/层结构。"""
    tree: dict = {}
    for rel, _abs_path in _iter_files(root):
        parts = rel.split(os.sep)
        if len(parts) > depth + 1:
            continue
        d = tree
        for p in parts[:-1]:  # 不含文件名
            d = d.setdefault(p, {})
    return tree


def search_repo(keyword: str, mode: str = "content", root: str | None = None,
                max_results: int = DEFAULT_MAX, context: int = DEFAULT_CONTEXT,
                depth: int = DEFAULT_DEPTH):
    """统一入口:mode ∈ {content, filename, modules}。

    返回 dict(与上游脚本一致的 JSON 结构)。
    """
    if root is None:
        from . import config
        root = config.PG_ROOT
    if not os.path.isdir(root):
        return {
            "status": "error",
            "message": f"PG_ROOT 目录不存在或不可访问: {root}。请配置 .env 中的 PG_ROOT 指向 pangu 代码根目录。",
        }
    if mode == "content":
        hits = search_content(keyword, root, max_results, context, depth)
        return {"status": "ok", "mode": "content", "count": len(hits), "hits": hits}
    if mode == "filename":
        hits = search_filename(keyword, root, max_results, depth)
        return {"status": "ok", "mode": "filename", "count": len(hits), "hits": hits}
    if mode == "modules":
        return {"status": "ok", "mode": "modules", "tree": list_modules(root, depth)}
    return {"status": "error", "message": f"未知模式: {mode}(应为 content/filename/modules)"}
