"""Voyage 最小连通性与响应维度检查。

该脚本会消耗两次 Voyage embedding 请求；仅在需要验证新 key 时手动运行：
  uv run python scripts/test_voyage_embedding.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from zhenyun_pangu_mcp.embedding.factory import create_embedding_provider  # noqa: E402


def main() -> int:
    try:
        provider = create_embedding_provider("voyage")
        document = provider.embed_documents(["采购询价单创建后，供应商进行报价"])
        query = provider.embed_query("如何查询询价供应商报价？")
        assert len(document) == 1
        assert len(document[0]) == 2048
        assert len(query) == 2048
    except Exception as exc:  # noqa: BLE001 - CLI 输出可操作错误
        print(f"❌ Voyage 连通性检查失败：{exc}")
        return 1
    print("document: 2048")
    print("query: 2048")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
